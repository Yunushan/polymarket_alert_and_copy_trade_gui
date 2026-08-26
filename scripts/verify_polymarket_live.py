from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore

from polymarket import bridge, clob_rest, data_api, gamma, relayer
from core.atomic_files import atomic_write_text
from polymarket.auth_readiness import build_clob_auth_readiness
from polymarket.constants import (
    POLYMARKET_BOUNDED_AUDIT_MUTATION_BLOCKER,
    POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED,
    POLYMARKET_CLOB_V2_MIGRATION_URL,
)
from polymarket.credential_runbook import build_polymarket_credential_runbook
from polymarket.live_verification import (
    ABSOLUTE_MAX_VERIFY_NOTIONAL,
    ABSOLUTE_MAX_VERIFY_SIZE,
    CONFIRM_LIVE_ORDER_CANCEL,
    LiveOrderCancelRequest,
    accepted_credential_read_checks,
    build_live_validation_stage_gates,
    load_allow_token_ids,
    run_live_order_cancel_verification,
)
from polymarket.live_report_schema import EXPECTED_REPOSITORY_ORIGIN
from polymarket.trader import PolymarketTrader, TraderConfig
from polymarket.ws_user import build_user_subscription, probe_user_websocket


RELAYER_HEADERS = ("RELAYER_API_KEY", "RELAYER_API_KEY_ADDRESS")
BUILDER_HEADERS = (
    "POLY_BUILDER_API_KEY",
    "POLY_BUILDER_TIMESTAMP",
    "POLY_BUILDER_PASSPHRASE",
    "POLY_BUILDER_SIGNATURE",
)

PUBLIC_CHECK_NAMES = (
    "clob_time",
    "gamma_markets",
    "data_leaderboard",
    "bridge_supported_assets",
)
PUBLIC_ONLY_PROFILE = "public-only"
PUBLIC_ONLY_WORKFLOW = ".github/workflows/ci.yml"
PUBLIC_ONLY_WORKFLOW_NAME = "CI"
PUBLIC_ONLY_FORBIDDEN_OPTIONS = frozenset(
    {
        "--skip-public-checks",
        "--skip-authenticated-read-checks",
        "--require-authenticated-read-ok",
        "--include-user-websocket-connect",
        "--user-ws-market",
        "--include-bridge-address-creation",
        "--bridge-address",
        "--to-chain-id",
        "--to-token-address",
        "--recipient-addr",
        "--allow-funded-order",
        "--cancel-immediately",
        "--confirm-live-order-cancel",
        "--allow-token-id",
        "--allow-token-file",
        "--token-id",
        "--side",
        "--price",
        "--size",
        "--tif",
        "--max-verify-size",
        "--max-verify-notional",
        "--maker-price-buffer",
        "--recovery-journal",
    }
)
PUBLIC_ONLY_SAFETY = {
    "dotenv_loaded": False,
    "credentials_present": False,
    "credential_variables_present": [],
    "authenticated_reads_attempted": False,
    "authenticated_user_websocket_attempted": False,
    "bridge_mutations_attempted": False,
    "funded_orders_attempted": False,
    "public_requests_read_only": True,
}
PUBLIC_ONLY_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "profile",
        "repository",
        "source_revision",
        "run_id",
        "run_attempt",
        "workflow",
        "workflow_name",
        "workflow_ref",
        "event",
        "runner_environment",
        "generated_at",
        "started_at",
        "completed_at",
    }
)
_GITHUB_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
STRICT_SOURCE_PROVENANCE_SCHEMA_VERSION = 1


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")


def _canonical_repository_origin(value: Any) -> str:
    normalized = str(value or "").strip().rstrip("/").lower()
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    accepted = {
        "https://github.com/yunushan/market-sentinel",
        "git@github.com:yunushan/market-sentinel",
        "ssh://git@github.com/yunushan/market-sentinel",
        "git://github.com/yunushan/market-sentinel",
    }
    return EXPECTED_REPOSITORY_ORIGIN if normalized in accepted else ""


def _scrubbed_git_environment() -> Dict[str, str]:
    """Return the minimum non-secret environment needed for local git reads."""

    allowed = (
        "PATH",
        "SystemRoot",
        "WINDIR",
        "ComSpec",
        "PATHEXT",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
    )
    environment = {name: os.environ[name] for name in allowed if os.environ.get(name)}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _run_git_readonly(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            *arguments,
        ],
        cwd=ROOT,
        env=_scrubbed_git_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _repository_source_state() -> Dict[str, Any]:
    """Return bounded git identity without exposing paths or command output."""

    try:
        top_level_result = _run_git_readonly("rev-parse", "--show-toplevel")
        revision_result = _run_git_readonly("rev-parse", "--verify", "HEAD")
        status_result = _run_git_readonly("status", "--porcelain=v1", "--untracked-files=all")
        origin_result = _run_git_readonly("remote", "get-url", "origin")
    except (OSError, subprocess.TimeoutExpired):
        return {
            "revision": "",
            "clean": False,
            "git_available": False,
            "repository_origin": "",
            "top_level_verified": False,
        }

    top_level_verified = False
    if top_level_result.returncode == 0:
        try:
            observed_top = Path(top_level_result.stdout.strip()).resolve(strict=True)
            top_level_verified = os.path.normcase(str(observed_top)) == os.path.normcase(
                str(ROOT.resolve(strict=True))
            )
        except (OSError, RuntimeError, ValueError):
            top_level_verified = False

    revision = revision_result.stdout.strip().lower() if revision_result.returncode == 0 else ""
    if not top_level_verified or not _COMMIT_RE.fullmatch(revision):
        revision = ""
    repository_origin = (
        _canonical_repository_origin(origin_result.stdout)
        if origin_result.returncode == 0
        else ""
    )
    return {
        "revision": revision,
        "clean": bool(top_level_verified and status_result.returncode == 0 and not status_result.stdout.strip()),
        "git_available": bool(top_level_verified and revision and status_result.returncode == 0),
        "repository_origin": repository_origin,
        "top_level_verified": top_level_verified,
    }


def _strict_source_provenance(
    initial: Mapping[str, Any],
    final: Mapping[str, Any],
) -> Dict[str, Any]:
    initial_revision = str(initial.get("revision") or "")
    final_revision = str(final.get("revision") or "")
    initial_origin = str(initial.get("repository_origin") or "")
    final_origin = str(final.get("repository_origin") or "")
    origin_bound = bool(
        initial_origin == EXPECTED_REPOSITORY_ORIGIN
        and final_origin == EXPECTED_REPOSITORY_ORIGIN
    )
    stable = bool(
        initial.get("clean") is True
        and final.get("clean") is True
        and origin_bound
        and _COMMIT_RE.fullmatch(initial_revision)
        and initial_revision == final_revision
    )
    return {
        "schema_version": STRICT_SOURCE_PROVENANCE_SCHEMA_VERSION,
        "repository": "market-sentinel",
        "repository_origin": EXPECTED_REPOSITORY_ORIGIN if origin_bound else "",
        "source_revision": initial_revision if stable else "",
        "initial_revision": initial_revision,
        "final_revision": final_revision,
        "initial_clean": initial.get("clean") is True,
        "final_clean": final.get("clean") is True,
        "stable": stable,
    }


def _funded_source_revision_gate(
    initial: Mapping[str, Any],
    immediately_before_funded: Mapping[str, Any],
) -> Dict[str, Any]:
    initial_revision = str(initial.get("revision") or "")
    current_revision = str(immediately_before_funded.get("revision") or "")
    initial_origin = str(initial.get("repository_origin") or "")
    current_origin = str(immediately_before_funded.get("repository_origin") or "")
    passed = bool(
        initial.get("clean") is True
        and immediately_before_funded.get("clean") is True
        and initial_origin == EXPECTED_REPOSITORY_ORIGIN
        and current_origin == EXPECTED_REPOSITORY_ORIGIN
        and _COMMIT_RE.fullmatch(initial_revision)
        and initial_revision == current_revision
    )
    return {
        "status": "pass" if passed else "fail",
        "clean": bool(immediately_before_funded.get("clean") is True),
        "matches_initial_revision": bool(initial_revision and initial_revision == current_revision),
        "source_revision": current_revision if passed else "",
        "repository_origin": current_origin if passed else "",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _public_only_credential_variables() -> list[str]:
    """Return credential-like environment variable names without exposing values."""
    generic_names = {"PRIVATE_KEY", "FUNDER_ADDRESS", "SIGNATURE_TYPE"}
    return sorted(
        name
        for name, value in os.environ.items()
        if value
        and (
            name in generic_names
            or name.startswith("POLY_")
            or name.startswith("POLYMARKET_")
            or name.startswith("RELAYER_")
        )
    )


def _present_option_names(argv: Sequence[str]) -> set[str]:
    return {token.split("=", 1)[0] for token in argv if token.startswith("--")}


def _public_only_expected_source(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "repository": args.source_repository,
        "source_revision": args.source_revision,
        "run_id": args.source_run_id,
        "run_attempt": args.source_run_attempt,
        "workflow_ref": args.source_workflow_ref,
    }


def _validate_public_only_invocation(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    argv: Sequence[str],
) -> None:
    if args.public_only and args.validate_public_only_report:
        parser.error("--public-only and --validate-public-only-report are mutually exclusive")

    forbidden_options = sorted(_present_option_names(argv) & PUBLIC_ONLY_FORBIDDEN_OPTIONS)
    if forbidden_options:
        parser.error("public-only mode rejects private or mutating options: " + ", ".join(forbidden_options))

    credential_variables = _public_only_credential_variables()
    if credential_variables:
        parser.error(
            "public-only mode refuses credential-bearing environments: " + ", ".join(credential_variables)
        )

    if not (0.0 < args.timeout <= 30.0):
        parser.error("public-only mode requires --timeout greater than 0 and at most 30 seconds")
    if args.public_only and not args.report_file:
        parser.error("--public-only requires --report-file for attestable evidence")
    if args.validate_public_only_report and args.report_file:
        parser.error("--report-file cannot be combined with --validate-public-only-report")
    if not _GITHUB_REPOSITORY_RE.fullmatch(args.source_repository or ""):
        parser.error("public-only mode requires --source-repository in owner/repository form")
    if not _COMMIT_RE.fullmatch(args.source_revision or ""):
        parser.error("public-only mode requires a lowercase 40-character --source-revision")
    if not isinstance(args.source_run_id, int) or args.source_run_id < 1:
        parser.error("public-only mode requires a positive --source-run-id")
    if not isinstance(args.source_run_attempt, int) or args.source_run_attempt < 1:
        parser.error("public-only mode requires a positive --source-run-attempt")

    expected_workflow_ref = f"{args.source_repository}/{PUBLIC_ONLY_WORKFLOW}@refs/heads/main"
    workflow_ref = args.source_workflow_ref or ""
    if workflow_ref != expected_workflow_ref:
        parser.error("--source-workflow-ref must bind the public workflow to refs/heads/main")


def _public_only_report_issues(
    report: Mapping[str, Any],
    *,
    expected_source: Mapping[str, Any],
    require_success: bool,
) -> list[str]:
    issues: list[str] = []
    expected_top_level = {"ok", "mode", "market_id", "evidence", "safety", "public_checks"}
    if set(report) != expected_top_level:
        issues.append("report must contain only the public-only top-level fields")
    if report.get("mode") != "public_only":
        issues.append("mode must be public_only")
    if report.get("market_id") != "polymarket":
        issues.append("market_id must be polymarket")

    safety = report.get("safety")
    if safety != PUBLIC_ONLY_SAFETY:
        issues.append("safety declaration does not prove the fail-closed public-only profile")

    public_checks = report.get("public_checks")
    checks_are_exact = isinstance(public_checks, Mapping) and set(public_checks) == set(PUBLIC_CHECK_NAMES)
    if not checks_are_exact:
        issues.append("public_checks must contain the exact reviewed public endpoint set")
        all_public_ok = False
    else:
        statuses = [public_checks[name].get("status") if isinstance(public_checks[name], Mapping) else None for name in PUBLIC_CHECK_NAMES]
        if any(status not in {"ok", "failed"} for status in statuses):
            issues.append("public checks may only report ok or failed in public-only mode")
        all_public_ok = all(status == "ok" for status in statuses)
    if report.get("ok") is not all_public_ok:
        issues.append("ok must exactly match the four public endpoint statuses")
    if require_success and not all_public_ok:
        issues.append("all public endpoints must pass before evidence can be attested")

    evidence = report.get("evidence")
    if not isinstance(evidence, Mapping):
        issues.append("evidence must be an object")
        return issues
    if set(evidence) != PUBLIC_ONLY_EVIDENCE_FIELDS:
        issues.append("evidence fields do not match the public-only schema")
    if evidence.get("schema_version") != 1:
        issues.append("evidence schema_version must be 1")
    if evidence.get("profile") != PUBLIC_ONLY_PROFILE:
        issues.append(f"evidence profile must be {PUBLIC_ONLY_PROFILE}")
    if evidence.get("workflow") != PUBLIC_ONLY_WORKFLOW:
        issues.append(f"evidence workflow must be {PUBLIC_ONLY_WORKFLOW}")
    if evidence.get("workflow_name") != PUBLIC_ONLY_WORKFLOW_NAME:
        issues.append(f"evidence workflow_name must be {PUBLIC_ONLY_WORKFLOW_NAME}")
    if evidence.get("event") != "workflow_dispatch":
        issues.append("evidence event must be workflow_dispatch")
    if evidence.get("runner_environment") != "github-hosted":
        issues.append("evidence runner_environment must be github-hosted")
    for name, expected in expected_source.items():
        if evidence.get(name) != expected:
            issues.append(f"evidence {name} does not match the expected GitHub source")

    generated_at = _parse_utc_timestamp(evidence.get("generated_at"))
    started_at = _parse_utc_timestamp(evidence.get("started_at"))
    completed_at = _parse_utc_timestamp(evidence.get("completed_at"))
    if not all((generated_at, started_at, completed_at)):
        issues.append("evidence timestamps must be timezone-aware ISO-8601 values")
    elif not (started_at <= completed_at <= generated_at):
        issues.append("evidence timestamps must be ordered started_at <= completed_at <= generated_at")
    return issues


def _write_report(path: str | Path, report: Mapping[str, Any]) -> None:
    atomic_write_text(Path(path), json.dumps(report, indent=2, sort_keys=True) + "\n")


def _is_link_like(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _enforce_windows_recovery_journal_privacy(_target: Path) -> None:
    if os.name == "nt":
        raise ValueError(
            "funded recovery journals are unavailable on Windows because this tool cannot "
            "verify an owner-only directory ACL; run the funded check from a private POSIX directory"
        )


def _validate_recovery_journal_target(value: str | Path) -> Path:
    target = Path(value).expanduser()
    if not target.is_absolute():
        raise ValueError("recovery journal path must be absolute")
    parent = target.parent
    if not parent.exists() or not parent.is_dir():
        raise ValueError("recovery journal parent directory must already exist")
    _enforce_windows_recovery_journal_privacy(target)
    current = parent
    while True:
        if _is_link_like(current):
            raise ValueError("recovery journal path cannot traverse a symlink or junction")
        if current == current.parent:
            break
        current = current.parent
    if target.exists():
        if _is_link_like(target) or not target.is_file():
            raise ValueError("recovery journal target must be a regular file")
    if os.name == "posix":
        if parent.stat().st_mode & 0o077:
            raise ValueError("recovery journal parent directory must not be group/world accessible")
        if target.exists() and target.stat().st_mode & 0o077:
            raise ValueError("existing recovery journal must not be group/world accessible")
    return target


@contextmanager
def _recovery_journal_session(
    value: str | Path,
    *,
    source_revision: str,
) -> Iterable[Callable[[Mapping[str, Any]], None]]:
    target = _validate_recovery_journal_target(value)
    lock_path = target.with_name(f".{target.name}.funded.lock")
    if _is_link_like(lock_path):
        raise ValueError("recovery journal lock path cannot be a symlink or junction")
    try:
        lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(
            "recovery journal is locked by another or interrupted funded run; reconcile it before removing the lock"
        ) from exc
    run_id = str(uuid.uuid4())
    started_at = _utc_now()
    sequence = 0
    try:
        lock_payload = json.dumps(
            {
                "run_id": run_id,
                "source_revision": source_revision,
                "started_at": started_at,
            },
            sort_keys=True,
        ).encode("utf-8")
        os.write(lock_descriptor, lock_payload)
        os.fsync(lock_descriptor)
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("existing recovery journal is unreadable and must be reconciled manually") from exc
            if not isinstance(existing, Mapping) or existing.get("resolved") is not True:
                raise ValueError("existing recovery journal is unresolved and must be reconciled before a new funded run")
            archive = target.with_name(
                f"{target.stem}.resolved-{run_id}{target.suffix or '.json'}"
            )
            target.replace(archive)
            os.chmod(archive, 0o600)

        def write(payload: Mapping[str, Any]) -> None:
            nonlocal sequence
            current_target = _validate_recovery_journal_target(target)
            sequence += 1
            journal = dict(payload)
            journal.update(
                {
                    "run_id": run_id,
                    "run_started_at": started_at,
                    "source_revision": source_revision,
                    "sequence": sequence,
                    "updated_at": _utc_now(),
                }
            )
            atomic_write_text(current_target, json.dumps(journal, indent=2, sort_keys=True) + "\n")
            os.chmod(current_target, 0o600)

        yield write
    finally:
        os.close(lock_descriptor)
        lock_path.unlink(missing_ok=True)


def _present(names: Iterable[str]) -> Dict[str, bool]:
    return {name: bool(os.getenv(name)) for name in names}


def _missing(names: Iterable[str]) -> list[str]:
    return [name for name in names if not os.getenv(name)]


def _headers(names: Iterable[str]) -> Dict[str, str]:
    return {name: os.getenv(name, "") for name in names if os.getenv(name)}


def _result(status: str, detail: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": status, "detail": detail}
    out.update(extra)
    return out


def _skipped(detail: str) -> Dict[str, Any]:
    return _result("skipped", detail)


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_server_time(value: Any) -> Dict[str, Any]:
    candidate = value.get("time", value.get("timestamp")) if isinstance(value, Mapping) else value
    if isinstance(candidate, bool):
        raise ValueError("CLOB server time is not a Unix timestamp")
    try:
        timestamp = float(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError("CLOB server time is not a Unix timestamp") from exc
    skew = abs(time.time() - timestamp)
    if not math.isfinite(timestamp) or skew > 24 * 60 * 60:
        raise ValueError("CLOB server time is not a current Unix timestamp")
    return {"semantic_check": "current_unix_time", "server_time_skew_seconds": round(skew, 3)}


def _validate_gamma_markets(value: Any) -> Dict[str, Any]:
    if not isinstance(value, list) or not value or not isinstance(value[0], Mapping):
        raise ValueError("Gamma markets response contains no market record")
    market = value[0]
    if not _nonblank(market.get("id")) or not any(
        _nonblank(market.get(field)) for field in ("question", "conditionId", "slug")
    ):
        raise ValueError("Gamma market record lacks its documented identity fields")
    return {"semantic_check": "market_identity", "records_observed": len(value)}


def _validate_leaderboard(value: Any) -> Dict[str, Any]:
    if not isinstance(value, list) or not value or not isinstance(value[0], Mapping):
        raise ValueError("Data leaderboard response contains no trader record")
    row = value[0]
    wallet = row.get("proxyWallet")
    if not isinstance(wallet, str) or re.fullmatch(r"0x[0-9A-Fa-f]{40}", wallet) is None:
        raise ValueError("Data leaderboard record lacks a documented proxy wallet")
    numeric_values = [row.get("pnl"), row.get("vol")]
    if not any(
        not isinstance(item, bool) and isinstance(item, (int, float)) and math.isfinite(float(item))
        for item in numeric_values
    ):
        raise ValueError("Data leaderboard record lacks a finite PnL or volume value")
    return {"semantic_check": "leaderboard_identity", "records_observed": len(value)}


def _validate_supported_assets(value: Any) -> Dict[str, Any]:
    assets = value.get("supportedAssets") if isinstance(value, Mapping) else None
    if not isinstance(assets, list) or not assets or not isinstance(assets[0], Mapping):
        raise ValueError("Bridge response contains no supported asset record")
    asset = assets[0]
    token = asset.get("token")
    if not _nonblank(str(asset.get("chainId", ""))) or not isinstance(token, Mapping) or not (
        _nonblank(token.get("symbol")) and (_nonblank(token.get("name")) or _nonblank(token.get("address")))
    ):
        raise ValueError("Bridge supported asset lacks its documented chain or token identity")
    return {"semantic_check": "supported_asset_identity", "records_observed": len(assets)}


def _probe(
    fn: Callable[[], Any],
    success_detail: str,
    validator: Callable[[Any], Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    try:
        value = fn()
        semantic = dict(validator(value)) if validator is not None else {}
        return _result("ok", success_detail, sample_type=type(value).__name__, **semantic)
    except Exception as exc:
        return _result("failed", type(exc).__name__, error_type=type(exc).__name__)


def _validate_clob_order_collection(value: Any) -> Dict[str, Any]:
    if not isinstance(value, list):
        raise ValueError("CLOB V2 authenticated order-list response must be a list")
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError("CLOB V2 authenticated order-list response contains a non-object record")
        if not any(_nonblank(row.get(name)) for name in ("id", "orderID", "order_id")):
            raise ValueError("CLOB V2 authenticated order-list response contains an unidentified order")
    return {"semantic_check": "authenticated_order_collection", "records_observed": len(value)}


def _validate_authenticated_list(value: Any) -> Dict[str, Any]:
    if not isinstance(value, list):
        raise ValueError("authenticated response must be a list")
    return {"semantic_check": "authenticated_collection", "records_observed": len(value)}


def _public_checks(timeout: float) -> Dict[str, Any]:
    return {
        "clob_time": _probe(
            lambda: clob_rest.get_server_time(timeout=timeout),
            "CLOB /time returned a current Unix timestamp.",
            _validate_server_time,
        ),
        "gamma_markets": _probe(
            lambda: gamma.list_markets(limit=1, timeout=timeout),
            "Gamma /markets returned an identified market.",
            _validate_gamma_markets,
        ),
        "data_leaderboard": _probe(
            lambda: data_api.get_leaderboard(limit=1, timeout=timeout),
            "Data /v1/leaderboard returned an identified trader row.",
            _validate_leaderboard,
        ),
        "bridge_supported_assets": _probe(
            lambda: bridge.get_supported_assets(timeout=timeout),
            "Bridge /supported-assets returned an identified supported asset.",
            _validate_supported_assets,
        ),
    }


def _skipped_public_checks() -> Dict[str, Any]:
    return {
        "clob_time": _skipped("Skipped by --skip-public-checks."),
        "gamma_markets": _skipped("Skipped by --skip-public-checks."),
        "data_leaderboard": _skipped("Skipped by --skip-public-checks."),
        "bridge_supported_assets": _skipped("Skipped by --skip-public-checks."),
    }


def _authenticated_read_checks(
    timeout: float,
    *,
    include_user_websocket_connect: bool = False,
    user_ws_markets: Iterable[str] = (),
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
    api_key = os.getenv("POLY_API_KEY")
    api_secret = os.getenv("POLY_API_SECRET") or os.getenv("POLY_SECRET")
    api_passphrase = os.getenv("POLY_PASSPHRASE")
    missing_sdk_credentials = []
    for present, label in (
        (private_key, "POLYMARKET_PRIVATE_KEY or PRIVATE_KEY"),
        (api_key, "POLY_API_KEY"),
        (api_secret, "POLY_API_SECRET or POLY_SECRET"),
        (api_passphrase, "POLY_PASSPHRASE"),
    ):
        if not present:
            missing_sdk_credentials.append(label)
    if missing_sdk_credentials:
        detail = "Missing explicit credentials for fresh py-clob-client-v2 authenticated reads."
        out["clob_l2_orders"] = _result("blocked", detail, missing=missing_sdk_credentials)
        out["py_clob_client_credentials"] = _result("blocked", detail, missing=missing_sdk_credentials)
    else:
        def read_open_orders() -> list[Any]:
            readiness = build_clob_auth_readiness()
            if readiness["blockers"]:
                raise ValueError("; ".join(readiness["blockers"]))
            trader = PolymarketTrader(
                TraderConfig(
                    private_key=str(private_key),
                    funder_address=os.getenv("POLYMARKET_FUNDER_ADDRESS") or os.getenv("FUNDER_ADDRESS") or None,
                    signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE") or os.getenv("SIGNATURE_TYPE") or "0"),
                    api_key=str(api_key),
                    api_secret=str(api_secret),
                    api_passphrase=str(api_passphrase),
                    authenticated_sdk_reads=True,
                    allow_api_key_derivation=False,
                    allow_api_key_creation=False,
                )
            )
            return trader.get_orders(only_first_page=True)

        out["clob_l2_orders"] = _probe(
            read_open_orders,
            "Freshly signed py-clob-client-v2 get_open_orders read responded.",
            _validate_clob_order_collection,
        )
        if out["clob_l2_orders"]["status"] == "ok":
            out["py_clob_client_credentials"] = _result(
                "ok",
                "py-clob-client-v2 completed a fresh authenticated read with explicit credentials.",
            )
        else:
            out["py_clob_client_credentials"] = _result(
                "blocked",
                "Read-only py-clob-client-v2 initialization or fresh authenticated read failed.",
                error_type=out["clob_l2_orders"].get("error_type", "unknown"),
            )

    if _missing(RELAYER_HEADERS):
        out["relayer_recent_transactions"] = _result(
            "blocked",
            "Missing relayer API key headers.",
            missing=_missing(RELAYER_HEADERS),
        )
    else:
        out["relayer_recent_transactions"] = _probe(
            lambda: relayer.get_recent_transactions(_headers(RELAYER_HEADERS), timeout=timeout),
            "Authenticated relayer recent transactions responded.",
            _validate_authenticated_list,
        )

    user_ws_auth = {
        "apiKey": os.getenv("POLY_API_KEY") or "",
        "secret": os.getenv("POLY_API_SECRET") or os.getenv("POLY_SECRET") or "",
        "passphrase": os.getenv("POLY_PASSPHRASE") or "",
    }
    user_ws_ready = False
    try:
        build_user_subscription(user_ws_auth)
        out["user_websocket_auth_payload"] = _result("ok", "User WebSocket auth payload can be built.")
        user_ws_ready = True
    except ValueError as exc:
        out["user_websocket_auth_payload"] = _result("blocked", str(exc))
    if include_user_websocket_connect and user_ws_ready:
        out["user_websocket_connect"] = _probe(
            lambda: probe_user_websocket(user_ws_auth, user_ws_markets, timeout=timeout),
            "Authenticated user WebSocket connected and subscription payload was sent.",
        )
    elif include_user_websocket_connect:
        out["user_websocket_connect"] = _result(
            "blocked",
            "Cannot open user WebSocket until apiKey, secret, and passphrase are present.",
        )
    else:
        out["user_websocket_connect"] = _skipped(
            "Not run. Pass --include-user-websocket-connect to open the authenticated user WebSocket.",
        )
    return out


def _skipped_authenticated_read_checks() -> Dict[str, Any]:
    return {
        "clob_l2_orders": _skipped("Skipped by --skip-authenticated-read-checks."),
        "py_clob_client_credentials": _skipped("Skipped by --skip-authenticated-read-checks."),
        "relayer_recent_transactions": _skipped("Skipped by --skip-authenticated-read-checks."),
        "user_websocket_auth_payload": _skipped("Skipped by --skip-authenticated-read-checks."),
        "user_websocket_connect": _skipped("Skipped by --skip-authenticated-read-checks."),
    }


def _bridge_address_checks(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.include_bridge_address_creation:
        return {
            "deposit_address_creation": _result(
                "blocked",
                "Not run. Pass --include-bridge-address-creation with --bridge-address for explicit address-creation verification.",
            ),
            "withdrawal_address_creation": _result(
                "blocked",
                "Not run. Pass --include-bridge-address-creation with withdrawal args for explicit address-creation verification.",
            ),
        }
    if not args.bridge_address:
        return {
            "deposit_address_creation": _result("blocked", "Missing --bridge-address."),
            "withdrawal_address_creation": _result("blocked", "Missing --bridge-address."),
        }
    out = {
        "deposit_address_creation": _probe(
            lambda: bridge.create_deposit_addresses(args.bridge_address, timeout=args.timeout),
            "Bridge deposit address creation responded.",
        )
    }
    required = (args.to_chain_id, args.to_token_address, args.recipient_addr)
    if all(required):
        out["withdrawal_address_creation"] = _probe(
            lambda: bridge.create_withdrawal_addresses(
                address=args.bridge_address,
                to_chain_id=args.to_chain_id,
                to_token_address=args.to_token_address,
                recipient_addr=args.recipient_addr,
                timeout=args.timeout,
            ),
            "Bridge withdrawal address creation responded.",
        )
    else:
        out["withdrawal_address_creation"] = _result(
            "blocked",
            "Missing --to-chain-id, --to-token-address, or --recipient-addr.",
        )
    return out


def _funded_order_check(args: argparse.Namespace, *, source_revision: str = "") -> Dict[str, Any]:
    if not any((args.token_id, args.side, args.price, args.size, args.allow_funded_order)):
        return _result(
            "blocked",
            "Not run. Pass token, side, price, and size for a dry-run transcript; add explicit execution flags for a real order/cancel check.",
        )
    if args.allow_funded_order and not POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED:
        return _result(
            "blocked",
            POLYMARKET_BOUNDED_AUDIT_MUTATION_BLOCKER,
            live_action=False,
            execution_supported=False,
            execution_protocol_required="CLOB V2",
            migration_reference=POLYMARKET_CLOB_V2_MIGRATION_URL,
            manual_reconciliation_required=False,
        )
    try:
        allow_tokens = load_allow_token_ids(args.allow_token_id or (), file_path=args.allow_token_file)
        request = LiveOrderCancelRequest(
            token_id=args.token_id or "",
            side=args.side or "",
            price=args.price,
            size=args.size,
            tif=args.tif,
            allow_token_ids=allow_tokens,
            private_key=os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PRIVATE_KEY") or "",
            funder_address=os.getenv("POLYMARKET_FUNDER_ADDRESS") or os.getenv("FUNDER_ADDRESS") or None,
            signature_type=os.getenv("POLYMARKET_SIGNATURE_TYPE") or os.getenv("SIGNATURE_TYPE") or "0",
            execute=bool(args.allow_funded_order),
            cancel_immediately=bool(args.cancel_immediately),
            confirmation=args.confirm_live_order_cancel or "",
            max_size=args.max_verify_size,
            max_notional=args.max_verify_notional,
            maker_price_buffer=args.maker_price_buffer,
        )
        if not args.allow_funded_order:
            return run_live_order_cancel_verification(request)
        with _recovery_journal_session(
            args.recovery_journal,
            source_revision=source_revision,
        ) as recovery_writer:
            return run_live_order_cancel_verification(
                request,
                recovery_writer=recovery_writer,
            )
    except Exception as exc:
        return _result(
            "failed",
            "Live order/cancel verification failed before a recovery-safe result could be produced.",
            error_type=type(exc).__name__,
            manual_reconciliation_required=bool(args.allow_funded_order),
        )


def _run_public_only(args: argparse.Namespace) -> int:
    started_at = _utc_now()
    public_checks = _public_checks(args.timeout)
    completed_at = _utc_now()
    report: Dict[str, Any] = {
        "ok": all(public_checks[name].get("status") == "ok" for name in PUBLIC_CHECK_NAMES),
        "mode": "public_only",
        "market_id": "polymarket",
        "evidence": {
            "schema_version": 1,
            "profile": PUBLIC_ONLY_PROFILE,
            "repository": args.source_repository,
            "source_revision": args.source_revision,
            "run_id": args.source_run_id,
            "run_attempt": args.source_run_attempt,
            "workflow": PUBLIC_ONLY_WORKFLOW,
            "workflow_name": PUBLIC_ONLY_WORKFLOW_NAME,
            "workflow_ref": args.source_workflow_ref,
            "event": "workflow_dispatch",
            "runner_environment": "github-hosted",
            "generated_at": _utc_now(),
            "started_at": started_at,
            "completed_at": completed_at,
        },
        "safety": dict(PUBLIC_ONLY_SAFETY),
        "public_checks": public_checks,
    }
    issues = _public_only_report_issues(
        report,
        expected_source=_public_only_expected_source(args),
        require_success=False,
    )
    if issues:
        raise RuntimeError("Invalid internally generated public-only report: " + "; ".join(issues))
    _write_report(args.report_file, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def _validate_public_only_report_file(args: argparse.Namespace) -> int:
    path = Path(args.validate_public_only_report)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Public-only report could not be read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("Public-only report must contain a JSON object.", file=sys.stderr)
        return 1
    issues = _public_only_report_issues(
        payload,
        expected_source=_public_only_expected_source(args),
        require_success=True,
    )
    if issues:
        print("Public-only report validation failed:\n- " + "\n- ".join(issues), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "validated_report": str(path),
                "source_revision": payload["evidence"]["source_revision"],
                "run_id": payload["evidence"]["run_id"],
                "run_attempt": payload["evidence"]["run_attempt"],
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Verify Polymarket public, credentialed, and optional live flows.")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--public-only",
        action="store_true",
        help="Run only the reviewed unauthenticated read-only endpoint profile without loading .env.",
    )
    parser.add_argument(
        "--validate-public-only-report",
        help="Revalidate one successful public-only report without making network requests.",
    )
    parser.add_argument("--source-repository")
    parser.add_argument("--source-revision")
    parser.add_argument("--source-run-id", type=int)
    parser.add_argument("--source-run-attempt", type=int)
    parser.add_argument("--source-workflow-ref")
    parser.add_argument("--skip-public-checks", action="store_true")
    parser.add_argument("--skip-authenticated-read-checks", action="store_true")
    parser.add_argument("--require-authenticated-read-ok", action="store_true")
    parser.add_argument("--include-user-websocket-connect", action="store_true")
    parser.add_argument("--user-ws-market", action="append", default=[])
    parser.add_argument("--report-file")
    parser.add_argument("--include-bridge-address-creation", action="store_true")
    parser.add_argument("--bridge-address")
    parser.add_argument("--to-chain-id")
    parser.add_argument("--to-token-address")
    parser.add_argument("--recipient-addr")
    parser.add_argument("--allow-funded-order", action="store_true")
    parser.add_argument("--cancel-immediately", action="store_true")
    parser.add_argument("--confirm-live-order-cancel")
    parser.add_argument("--allow-token-id", action="append", default=[])
    parser.add_argument("--allow-token-file")
    parser.add_argument("--token-id")
    parser.add_argument("--side", choices=["BUY", "SELL"])
    parser.add_argument("--price")
    parser.add_argument("--size")
    parser.add_argument("--tif", default="GTC")
    parser.add_argument("--max-verify-size", type=float, default=ABSOLUTE_MAX_VERIFY_SIZE)
    parser.add_argument("--max-verify-notional", type=float, default=ABSOLUTE_MAX_VERIFY_NOTIONAL)
    parser.add_argument("--maker-price-buffer", type=float, default=0.005)
    parser.add_argument(
        "--recovery-journal",
        help="Absolute path to a private, atomically updated funded-order recovery journal.",
    )
    args = parser.parse_args(raw_argv)

    public_only_mode = bool(args.public_only or args.validate_public_only_report)
    source_options = {
        "--source-repository",
        "--source-revision",
        "--source-run-id",
        "--source-run-attempt",
        "--source-workflow-ref",
    }
    if not public_only_mode and _present_option_names(raw_argv) & source_options:
        parser.error("--source-* metadata options are reserved for public-only evidence")
    if public_only_mode:
        _validate_public_only_invocation(parser, args, raw_argv)
        if args.validate_public_only_report:
            return _validate_public_only_report_file(args)
        return _run_public_only(args)

    if POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED and args.allow_funded_order and not args.recovery_journal:
        parser.error("--allow-funded-order requires --recovery-journal")
    if args.recovery_journal and not args.allow_funded_order:
        parser.error("--recovery-journal is only valid with --allow-funded-order")
    if POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED and args.allow_funded_order:
        try:
            recovery_target = _validate_recovery_journal_target(args.recovery_journal)
        except ValueError as exc:
            parser.error(str(exc))
        if args.report_file and recovery_target.resolve(strict=False) == Path(args.report_file).resolve(strict=False):
            parser.error("--recovery-journal and --report-file must be different files")

    initial_source_state = _repository_source_state()
    _load_env()
    public_checks = _skipped_public_checks() if args.skip_public_checks else _public_checks(args.timeout)
    authenticated_checks = (
        _skipped_authenticated_read_checks()
        if args.skip_authenticated_read_checks
        else _authenticated_read_checks(
            args.timeout,
            include_user_websocket_connect=args.include_user_websocket_connect,
            user_ws_markets=args.user_ws_market,
        )
    )
    accepted_reads = accepted_credential_read_checks(authenticated_checks)
    bridge_checks = _bridge_address_checks(args)
    pre_funded_source_state = _repository_source_state() if args.allow_funded_order else initial_source_state
    funded_source_gate = _funded_source_revision_gate(initial_source_state, pre_funded_source_state)
    if args.allow_funded_order and not POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED:
        funded_check = _funded_order_check(args)
        funded_check["source_revision_gate"] = funded_source_gate
    elif args.allow_funded_order and not accepted_reads:
        funded_check = _result(
            "blocked",
            "Funded execution requires a successful accepted non-mutating authenticated read in this run.",
            live_action=False,
            manual_reconciliation_required=False,
            accepted_credential_read_checks=accepted_reads,
            source_revision_gate=funded_source_gate,
        )
    elif args.allow_funded_order and funded_source_gate["status"] != "pass":
        funded_check = _result(
            "blocked",
            "Funded execution requires an exact clean committed source revision.",
            live_action=False,
            manual_reconciliation_required=False,
            accepted_credential_read_checks=accepted_reads,
            source_revision_gate=funded_source_gate,
        )
    else:
        funded_check = _funded_order_check(
            args,
            source_revision=str(funded_source_gate.get("source_revision") or ""),
        )
        if args.allow_funded_order:
            funded_check["source_revision_gate"] = funded_source_gate

    final_source_state = _repository_source_state()
    report = {
        "ok": True,
        "generated_at": time.time(),
        "mode": "strict_cli",
        "market_id": "polymarket",
        "source_provenance": _strict_source_provenance(initial_source_state, final_source_state),
        "credential_presence": {
            "clob_sdk_read_credentials": {
                "POLYMARKET_PRIVATE_KEY or PRIVATE_KEY": bool(
                    os.getenv("POLYMARKET_PRIVATE_KEY") or os.getenv("PRIVATE_KEY")
                ),
                "POLY_API_KEY": bool(os.getenv("POLY_API_KEY")),
                "POLY_API_SECRET or POLY_SECRET": bool(
                    os.getenv("POLY_API_SECRET") or os.getenv("POLY_SECRET")
                ),
                "POLY_PASSPHRASE": bool(os.getenv("POLY_PASSPHRASE")),
            },
            "py_clob_client": _present(("POLYMARKET_PRIVATE_KEY", "PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS", "FUNDER_ADDRESS", "POLYMARKET_SIGNATURE_TYPE", "SIGNATURE_TYPE")),
            "relayer_headers": _present(RELAYER_HEADERS),
            "builder_headers": _present(BUILDER_HEADERS),
            "user_ws": _present(("POLY_API_KEY", "POLY_API_SECRET", "POLY_SECRET", "POLY_PASSPHRASE")),
        },
        "clob_auth_readiness": build_clob_auth_readiness(),
        "credential_runbook": build_polymarket_credential_runbook(),
        "live_order_cancel_harness": {
            "default_mode": "dry_run_transcript",
            "execution_supported": POLYMARKET_BOUNDED_AUDIT_MUTATIONS_SUPPORTED,
            "execution_protocol_required": "CLOB V2",
            "migration_reference": POLYMARKET_CLOB_V2_MIGRATION_URL,
            "blocker": POLYMARKET_BOUNDED_AUDIT_MUTATION_BLOCKER,
            "execute_flag": "--allow-funded-order",
            "confirmation_required": CONFIRM_LIVE_ORDER_CANCEL,
            "hard_max_size": ABSOLUTE_MAX_VERIFY_SIZE,
            "hard_max_notional": ABSOLUTE_MAX_VERIFY_NOTIONAL,
        },
        "public_checks": public_checks,
        "authenticated_read_checks": authenticated_checks,
        "bridge_address_checks": bridge_checks,
        "funded_live_order_check": funded_check,
    }
    report["stage_gates"] = build_live_validation_stage_gates(report)
    report["ok"] = not any(
        item.get("status") == "failed"
        for section in (
            report["public_checks"],
            report["authenticated_read_checks"],
            report["bridge_address_checks"],
        )
        for item in section.values()
    ) and report["funded_live_order_check"].get("status") != "failed"
    if args.allow_funded_order and report["funded_live_order_check"].get("status") != "ok":
        report["ok"] = False
        report["stage_gates"]["required_funded_execution"] = "failed"
    if args.require_authenticated_read_ok and not report["stage_gates"]["credentialed_read_ok"]:
        report["ok"] = False
        report["stage_gates"]["required_authenticated_read"] = "failed"
    if report["source_provenance"].get("stable") is not True:
        report["ok"] = False
        report["stage_gates"]["exact_source_revision"] = "failed"
    if args.report_file:
        _write_report(args.report_file, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
