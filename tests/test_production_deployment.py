from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from stat import S_IFREG
from unittest.mock import call, patch
from urllib.error import HTTPError
from urllib.request import Request

from core.deployment_identity import frontend_tree_sha256
from scripts.backup_state import create_backup
from scripts import verify_production_deployment as deployment
from scripts.verify_production_deployment import (
    DURABLE_STATE_PATHS,
    PUBLIC_PROXY_AUTH_PROBES,
    check_backup_evidence,
    check_durable_state_wiring,
    check_evidence_output_directory,
    check_filesystem_permissions,
    check_loopback,
    check_loopback_metrics,
    check_loopback_token_auth,
    check_public_proxy,
    check_source_revision,
    check_systemd,
    _fsync_parent_directory,
    _RejectRedirects,
    _validated_public_origin,
    build_evidence,
    source_identity,
    main,
    write_evidence,
)

TEST_SOURCE_REVISION = "a" * 40
TEST_FRONTEND_SHA256 = "b" * 64


class _Response:
    status = 200

    def __init__(self, headers: dict[str, str], payload: dict[str, str]) -> None:
        self.headers = headers
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _unauthorized_errors(*, bodies: list[io.BytesIO] | None = None) -> list[HTTPError]:
    if bodies is None:
        bodies = [io.BytesIO(b'{"error":"unauthorized"}') for _ in PUBLIC_PROXY_AUTH_PROBES]
    return [
        HTTPError(
            f"https://analytics.example.com/{relative_url}",
            401,
            "Unauthorized",
            {},
            body,
        )
        for (_, relative_url), body in zip(PUBLIC_PROXY_AUTH_PROBES, bodies, strict=True)
    ]


class ProductionDeploymentTests(unittest.TestCase):
    def test_evidence_includes_a_versioned_utc_collection_timestamp(self) -> None:
        evidence = build_evidence(
            [{"name": "loopback_health", "status": "pass"}],
            collected_at=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
            source={
                "project_version": "1.0.11",
                "git_revision": "a" * 40,
                "git_revision_status": "ok",
                "git_worktree_status": "clean",
            },
        )

        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(evidence["collected_at"], "2026-07-19T12:00:00Z")
        self.assertEqual(evidence["source"]["project_version"], "1.0.11")
        self.assertEqual(evidence["source"]["git_revision"], "a" * 40)
        self.assertEqual(evidence["status"], "ok")

    def test_source_identity_records_only_a_valid_git_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nversion = '1.2.3'\n", encoding="utf-8")
            with patch(
                "scripts.verify_production_deployment.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(["git"], 0, f"{root.resolve()}\n", ""),
                    subprocess.CompletedProcess(["git"], 0, "a" * 40 + "\n", ""),
                    subprocess.CompletedProcess(["git"], 0, "", ""),
                    subprocess.CompletedProcess(["git"], 0, "a" * 40 + "\n", ""),
                ],
            ):
                identity = source_identity(root)

        self.assertEqual(identity, {
            "project_version": "1.2.3",
            "git_revision": "a" * 40,
            "git_revision_status": "ok",
            "git_worktree_status": "clean",
        })

    def test_source_identity_does_not_retain_invalid_git_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nversion = '1.2.3'\n", encoding="utf-8")
            with patch(
                "scripts.verify_production_deployment.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(["git"], 0, f"{root.resolve()}\n", ""),
                    subprocess.CompletedProcess(["git"], 0, "credential-like-output\n", ""),
                ],
            ):
                identity = source_identity(root)

        self.assertEqual(identity["project_version"], "1.2.3")
        self.assertEqual(identity["git_revision"], "")
        self.assertEqual(identity["git_revision_status"], "unavailable")
        self.assertEqual(identity["git_worktree_status"], "unavailable")

    def test_source_identity_rejects_head_changes_and_scrubs_git_environment(self) -> None:
        before = "a" * 40
        after = "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "pyproject.toml").write_text("[project]\nversion = '1.2.3'\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"PATH": os.environ.get("PATH", ""), "GIT_DIR": "attacker", "git_work_tree": "shadow"},
                clear=True,
            ):
                with patch(
                    "scripts.verify_production_deployment.subprocess.run",
                    side_effect=[
                        subprocess.CompletedProcess(["git"], 0, f"{root}\n", ""),
                        subprocess.CompletedProcess(["git"], 0, before + "\n", ""),
                        subprocess.CompletedProcess(["git"], 0, "", ""),
                        subprocess.CompletedProcess(["git"], 0, after + "\n", ""),
                    ],
                ) as runner:
                    identity = source_identity(root)

            self.assertEqual(runner.call_count, 4)
            for call in runner.call_args_list:
                environment = call.kwargs["env"]
                self.assertFalse(any(name.upper().startswith("GIT_") for name in environment))
                self.assertEqual(call.kwargs["cwd"], root)

        self.assertEqual(identity["git_revision"], "")
        self.assertEqual(identity["git_revision_status"], "invalid")
        self.assertEqual(identity["git_worktree_status"], "unavailable")
        self.assertEqual(check_source_revision(before, identity)["status"], "fail")

    def test_source_identity_rejects_a_different_git_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            other = root / "other"
            other.mkdir()
            (root / "pyproject.toml").write_text("[project]\nversion = '1.2.3'\n", encoding="utf-8")
            with patch(
                "scripts.verify_production_deployment.subprocess.run",
                return_value=subprocess.CompletedProcess(["git"], 0, f"{other}\n", ""),
            ) as runner:
                identity = source_identity(root)

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(identity["git_revision"], "")
        self.assertEqual(identity["git_revision_status"], "invalid")
        self.assertEqual(identity["git_worktree_status"], "unavailable")

    def test_source_identity_and_revision_check_reject_dirty_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nversion = '1.2.3'\n", encoding="utf-8")
            tracked = root / "web_api.py"
            tracked.write_text("SAFE = True\n", encoding="utf-8")
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Deployment Audit",
                    "-c",
                    "user.email=deployment-audit@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "initial",
                ],
                cwd=root,
                check=True,
            )
            clean_identity = source_identity(root)
            revision = clean_identity["git_revision"]
            self.assertEqual(clean_identity["git_worktree_status"], "clean")
            self.assertEqual(check_source_revision(revision, clean_identity)["status"], "pass")

            tracked.write_text("SAFE = False\n", encoding="utf-8")
            (root / "untracked.py").write_text("SHADOW = True\n", encoding="utf-8")
            dirty_identity = source_identity(root)

        self.assertEqual(dirty_identity["git_revision"], revision)
        self.assertEqual(dirty_identity["git_worktree_status"], "dirty")
        dirty_check = check_source_revision(revision, dirty_identity)
        self.assertEqual(dirty_check["status"], "fail")
        self.assertIn("tracked, staged, and untracked", dirty_check["detail"])

    def test_verifier_runs_when_invoked_as_a_script_path(self) -> None:
        script = Path(__file__).resolve().parent.parent / "scripts" / "verify_production_deployment.py"
        result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("production deployment evidence", result.stdout)
        self.assertIn("--backup-directory", result.stdout)

    def test_systemd_checks_require_active_enabled_and_recent_backup(self) -> None:
        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[1] == "show":
                return subprocess.CompletedProcess(args, 0, "success\n0\nThu 1970-01-01 00:16:39 UTC\n", "")
            return subprocess.CompletedProcess(args, 0, "active\n", "")

        checks = check_systemd(runner, clock=lambda: 1000.0)
        self.assertEqual(len(checks), 7)
        self.assertTrue(all(check["status"] == "pass" for check in checks))

    def test_backup_evidence_opens_and_cryptographically_verifies_a_recent_pair(self) -> None:
        now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "state"
            backup_directory = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            with patch("scripts.backup_state._utc_now", return_value=now):
                manifest = create_backup(source, backup_directory)
            archive = backup_directory / str(manifest["archive"])
            private_directory = SimpleNamespace(st_mode=0o040700, st_uid=123)

            check = check_backup_evidence(
                backup_directory,
                clock=lambda: (now + timedelta(hours=1)).timestamp(),
                stat_reader=lambda _path: private_directory,
            )
            self.assertEqual(check["status"], "pass")
            self.assertEqual(check["archive"], archive.name)
            self.assertEqual(check["sha256"], manifest["sha256"])
            self.assertEqual(check["verified_pairs"], 1)
            self.assertEqual(check["verified_archive_bytes"], archive.stat().st_size)
            self.assertGreater(check["verified_tar_bytes"], check["verified_bytes"])

            with archive.open("ab") as handle:
                handle.write(b"tampered")
            failed = check_backup_evidence(
                backup_directory,
                clock=lambda: (now + timedelta(hours=1)).timestamp(),
                stat_reader=lambda _path: private_directory,
            )

        self.assertEqual(failed["status"], "fail")
        self.assertEqual(failed["verified_pairs"], 0)
        self.assertEqual(failed["invalid_pairs"], 1)

    def test_backup_evidence_rejects_stale_and_untrusted_directories(self) -> None:
        now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "state"
            backup_directory = root / "backups"
            source.mkdir()
            source.joinpath("config.json").write_text("{}", encoding="utf-8")
            with patch("scripts.backup_state._utc_now", return_value=now):
                create_backup(source, backup_directory)

            private_directory = SimpleNamespace(st_mode=0o040700, st_uid=123)
            stale = check_backup_evidence(
                backup_directory,
                clock=lambda: (now + timedelta(hours=27)).timestamp(),
                stat_reader=lambda _path: private_directory,
            )
            untrusted = check_backup_evidence(
                backup_directory,
                clock=now.timestamp,
                stat_reader=lambda _path: SimpleNamespace(st_mode=0o040770, st_uid=123),
            )

        self.assertEqual(stale["status"], "fail")
        self.assertEqual(stale["verified_pairs"], 1)
        self.assertIn("no cryptographically verified backup pair", stale["detail"])
        self.assertEqual(untrusted["status"], "fail")
        self.assertIn("trusted private directory", untrusted["detail"])
        relative = check_backup_evidence(
            Path("relative-backups"),
            clock=now.timestamp,
            stat_reader=lambda _path: SimpleNamespace(st_mode=0o040700, st_uid=123),
        )
        self.assertEqual(relative["status"], "fail")
        self.assertIn("absolute path", relative["detail"])

    def test_filesystem_check_requires_private_paths_and_root_owned_environment(self) -> None:
        paths = {
            "market-sentinel.env": SimpleNamespace(st_mode=0o100600, st_uid=0),
            "market-sentinel": SimpleNamespace(st_mode=0o040700, st_uid=123),
            "market-sentinel-backups": SimpleNamespace(st_mode=0o040700, st_uid=123),
        }
        checks = check_filesystem_permissions(lambda path: paths[path.name])
        self.assertTrue(all(check["status"] == "pass" for check in checks))

        paths["market-sentinel.env"] = SimpleNamespace(st_mode=0o100640, st_uid=123)
        environment = check_filesystem_permissions(lambda path: paths[path.name])[0]
        self.assertEqual(environment["status"], "fail")

    def test_durable_state_wiring_proves_running_paths_and_backup_source(self) -> None:
        properties = {
            ("market-sentinel-web.service", "EnvironmentFiles"): (
                "/etc/market-sentinel/market-sentinel.env (ignore_errors=no)"
            ),
            ("market-sentinel-web.service", "ReadWritePaths"): "/var/lib/market-sentinel",
            ("market-sentinel-backup.service", "ExecStart"): (
                "{ path=/opt/market-sentinel/.venv/bin/python ; "
                "argv[]=/opt/market-sentinel/.venv/bin/python scripts/backup_state.py "
                "--source /var/lib/market-sentinel --destination /var/lib/market-sentinel-backups ; }"
            ),
            ("market-sentinel-web.service", "MainPID"): "4242",
        }

        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            key = (args[2], args[3].removeprefix("--property="))
            return subprocess.CompletedProcess(args, 0, properties[key] + "\n", "")

        process_environment = b"\0".join(
            [
                b"UNRELATED_SECRET=must-not-appear",
                *[
                    f"{name}={path.as_posix()}".encode("utf-8")
                    for name, path in DURABLE_STATE_PATHS.items()
                ],
            ]
        )
        check = check_durable_state_wiring(runner, lambda pid: process_environment)

        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["durable_store_count"], 4)
        self.assertEqual(check["backup_source"], "/var/lib/market-sentinel")
        self.assertNotIn("must-not-appear", check["detail"])

    def test_durable_state_wiring_fails_closed_for_an_unsafe_runtime_path(self) -> None:
        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            property_name = args[3].removeprefix("--property=")
            values = {
                "EnvironmentFiles": "/etc/market-sentinel/market-sentinel.env (ignore_errors=no)",
                "ReadWritePaths": "/var/lib/market-sentinel",
                "ExecStart": "--source /var/lib/market-sentinel --destination /var/lib/backups",
                "MainPID": "17",
            }
            return subprocess.CompletedProcess(args, 0, values[property_name] + "\n", "")

        process_environment = b"\0".join(
            [
                f"{name}={('/opt/market-sentinel/data/cache.json' if name == 'POLYMARKET_ANALYTICS_CACHE_PATH' else path.as_posix())}".encode(
                    "utf-8"
                )
                for name, path in DURABLE_STATE_PATHS.items()
            ]
        )
        check = check_durable_state_wiring(runner, lambda pid: process_environment)

        self.assertEqual(check["status"], "fail")
        self.assertIn("unsafe POLYMARKET_ANALYTICS_CACHE_PATH", check["detail"])

    def test_durable_state_wiring_fails_when_backup_does_not_cover_state_root(self) -> None:
        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            property_name = args[3].removeprefix("--property=")
            values = {
                "EnvironmentFiles": "/etc/market-sentinel/market-sentinel.env (ignore_errors=no)",
                "ReadWritePaths": "/var/lib/market-sentinel",
                "ExecStart": "--source /opt/market-sentinel/data --destination /var/lib/backups",
                "MainPID": "17",
            }
            return subprocess.CompletedProcess(args, 0, values[property_name] + "\n", "")

        check = check_durable_state_wiring(runner, lambda pid: b"")

        self.assertEqual(check["status"], "fail")
        self.assertIn("does not capture the durable state directory", check["detail"])

    def test_durable_state_wiring_rejects_an_optional_service_environment(self) -> None:
        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args,
                0,
                "/etc/market-sentinel/market-sentinel.env (ignore_errors=yes)\n",
                "",
            )

        check = check_durable_state_wiring(runner, lambda pid: b"")

        self.assertEqual(check["status"], "fail")
        self.assertIn("optional instead of fail-fast", check["detail"])

    @unittest.skipUnless(os.name == "posix", "symbolic-link safety is verified on POSIX hosts")
    def test_filesystem_check_rejects_a_symlinked_critical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target.env"
            target.write_text("token=not-read", encoding="utf-8")
            linked = root / "market-sentinel.env"
            linked.symlink_to(target)

            with patch.object(deployment, "REQUIRED_PRIVATE_PATHS", ((linked, S_IFREG, False),)):
                check = check_filesystem_permissions(backup_directory=root)[0]

        self.assertEqual(check["status"], "fail")
        self.assertIn("expected=file", check["detail"])

    def test_evidence_output_requires_a_private_root_owned_parent_directory(self) -> None:
        # Do not use /var here: macOS deliberately exposes it as a compatibility
        # symlink, while this unit test supplies its own directory metadata.
        output = Path.cwd().resolve() / "market-sentinel-evidence-test" / "deployment.json"
        metadata = SimpleNamespace(st_mode=0o040700, st_uid=0)
        self.assertEqual(check_evidence_output_directory(output, lambda path: metadata)["status"], "pass")

        untrusted = SimpleNamespace(st_mode=0o040700, st_uid=123)
        self.assertEqual(check_evidence_output_directory(output, lambda path: untrusted)["status"], "fail")

    @unittest.skipUnless(os.name == "posix", "symbolic-link safety is verified on POSIX hosts")
    def test_evidence_output_rejects_a_symlinked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            trusted.mkdir()
            linked = root / "service-controlled-link"
            linked.symlink_to(trusted, target_is_directory=True)
            metadata = SimpleNamespace(st_mode=0o040700, st_uid=0)

            check = check_evidence_output_directory(linked / "deployment.json", lambda path: metadata)

            self.assertEqual(check["status"], "fail")
            self.assertIn("symbolic-link", check["detail"])

    @unittest.skipUnless(os.name == "posix", "symbolic-link safety is verified on POSIX hosts")
    def test_evidence_output_rejects_a_symlinked_ancestor_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            trusted.mkdir()
            linked = root / "service-controlled-link"
            linked.symlink_to(trusted, target_is_directory=True)
            metadata = SimpleNamespace(st_mode=0o040700, st_uid=0)

            check = check_evidence_output_directory(linked / "nested" / "deployment.json", lambda path: metadata)

            self.assertEqual(check["status"], "fail")
            self.assertIn(str(linked), check["detail"])

    def test_systemd_check_rejects_a_stale_backup(self) -> None:
        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[1] == "show":
                return subprocess.CompletedProcess(args, 0, "success\n0\nThu 1970-01-01 00:00:01 UTC\n", "")
            return subprocess.CompletedProcess(args, 0, "active\n", "")

        checks = check_systemd(runner, clock=lambda: 1_000_000.0)
        backup = checks[-1]
        self.assertEqual(backup["status"], "fail")
        self.assertIn("backup_age_seconds=999999", backup["detail"])

    def test_systemd_check_rejects_an_impossibly_future_backup_timestamp(self) -> None:
        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            if args[1] == "show":
                return subprocess.CompletedProcess(args, 0, "success\n0\nThu 1970-01-01 00:23:20 UTC\n", "")
            return subprocess.CompletedProcess(args, 0, "active\n", "")

        checks = check_systemd(runner, clock=lambda: 1000.0)
        backup = checks[-1]
        self.assertEqual(backup["status"], "fail")
        self.assertIn("backup_age_seconds=-400", backup["detail"])

    def test_loopback_checks_expected_version(self) -> None:
        with patch("scripts.verify_production_deployment.check_health", return_value={"api_version": "1.0.10"}):
            self.assertEqual(check_loopback("http://127.0.0.1", "", 1.0, "1.0.10")["status"], "pass")
            with self.assertRaisesRegex(RuntimeError, "expected 1.0.11"):
                check_loopback("http://127.0.0.1", "", 1.0, "1.0.11")

    def test_loopback_binds_the_running_process_and_served_frontend_to_reviewed_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frontend = Path(directory)
            index = frontend / "index.html"
            index.write_text("reviewed", encoding="utf-8")
            digest = frontend_tree_sha256(frontend)
            payload = {
                "api_version": "1.0.10",
                "runtime_source_revision": TEST_SOURCE_REVISION,
                "runtime_frontend_sha256": digest,
            }
            with patch("scripts.verify_production_deployment.check_health", return_value=payload):
                result = check_loopback(
                    "http://127.0.0.1",
                    "token",
                    1.0,
                    "1.0.10",
                    TEST_SOURCE_REVISION,
                    digest,
                    frontend,
                )
                self.assertEqual(result["runtime_source_revision"], TEST_SOURCE_REVISION)
                self.assertEqual(result["disk_frontend_sha256"], digest)

                with self.assertRaisesRegex(RuntimeError, "restart the service"):
                    check_loopback(
                        "http://127.0.0.1",
                        "token",
                        1.0,
                        "1.0.10",
                        "c" * 40,
                        digest,
                        frontend,
                    )

                index.write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "served frontend tree digest"):
                    check_loopback(
                        "http://127.0.0.1",
                        "token",
                        1.0,
                        "1.0.10",
                        TEST_SOURCE_REVISION,
                        digest,
                        frontend,
                    )

    def test_loopback_metrics_requires_prometheus_format_and_required_counters(self) -> None:
        good_headers = {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}
        good_metrics = "\n".join(
            [
                "market_sentinel_http_requests_total 1",
                "market_sentinel_http_request_duration_seconds_total 0.100000",
                "market_sentinel_http_requests_completed_total 1",
            ]
        )

        class MetricsResponse:
            status = 200
            headers = good_headers

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return good_metrics.encode("utf-8")

        with patch("scripts.verify_production_deployment.urlopen", return_value=MetricsResponse()):
            self.assertEqual(check_loopback_metrics("http://127.0.0.1:8765/metrics", "token", 1.0)["status"], "pass")

        missing_metric = good_metrics.replace("market_sentinel_http_requests_completed_total 1", "")
        class MissingMetricResponse(MetricsResponse):
            def read(self):
                return missing_metric.encode("utf-8")

        with patch("scripts.verify_production_deployment.urlopen", return_value=MissingMetricResponse()):
            with self.assertRaisesRegex(RuntimeError, "missing required metrics"):
                check_loopback_metrics("http://127.0.0.1:8765/metrics", "", 1.0)

    def test_source_revision_requires_an_exact_release_commit(self) -> None:
        revision = "a" * 40
        self.assertEqual(
            "pass",
            check_source_revision(
                revision,
                {"git_revision": revision, "git_revision_status": "ok", "git_worktree_status": "clean"},
            )["status"],
        )
        mismatch = check_source_revision(
            revision,
            {"git_revision": "b" * 40, "git_revision_status": "ok", "git_worktree_status": "clean"},
        )
        self.assertEqual("fail", mismatch["status"])
        self.assertIn("expected", mismatch["detail"])
        dirty = check_source_revision(
            revision,
            {"git_revision": revision, "git_revision_status": "ok", "git_worktree_status": "dirty"},
        )
        self.assertEqual("fail", dirty["status"])
        self.assertIn("not clean", dirty["detail"])
        invalid = check_source_revision(
            "not-a-commit",
            {"git_revision": revision, "git_revision_status": "ok", "git_worktree_status": "clean"},
        )
        self.assertEqual("fail", invalid["status"])
        self.assertIn("40-character Git commit", invalid["detail"])

    def test_evidence_output_is_atomic_json_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence" / "deployment.json"
            output.parent.mkdir()
            write_evidence(output, {"status": "ok", "checks": [{"name": "loopback", "status": "pass"}]})

            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "ok")
            if os.name == "posix":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(output.parent.glob("*.tmp")))

    def test_evidence_parent_directory_is_synced_on_posix(self) -> None:
        path = Path("evidence") / "deployment.json"
        with (
            patch("scripts.verify_production_deployment.os.name", "posix"),
            patch("scripts.verify_production_deployment.os.open", return_value=42) as open_directory,
            patch("scripts.verify_production_deployment.os.fsync") as sync,
            patch("scripts.verify_production_deployment.os.close") as close,
        ):
            _fsync_parent_directory(path)

        open_directory.assert_called_once()
        sync.assert_called_once_with(42)
        close.assert_called_once_with(42)

    @unittest.skipUnless(os.name == "posix", "symlink safety is verified on POSIX hosts")
    def test_evidence_output_ignores_a_predictable_temp_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence" / "deployment.json"
            output.parent.mkdir()
            protected = Path(tmp) / "protected.txt"
            protected.write_text("do not overwrite", encoding="utf-8")
            predictable = output.parent / f".{output.name}.{os.getpid()}.tmp"
            predictable.symlink_to(protected)

            write_evidence(output, {"status": "ok", "checks": []})

            self.assertEqual(protected.read_text(encoding="utf-8"), "do not overwrite")
            self.assertTrue(predictable.is_symlink())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "ok")

    def test_verifier_fails_when_evidence_output_cannot_be_written(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "verify_production_deployment.py",
                    "--skip-systemd",
                    "--expected-version",
                    "1.0.11",
                    "--expected-source-revision",
                    TEST_SOURCE_REVISION,
                    "--expected-frontend-sha256",
                    TEST_FRONTEND_SHA256,
                    "--output",
                    "deployment.json",
                ],
            ),
            patch("scripts.verify_production_deployment.check_loopback", return_value={"name": "loopback_health", "status": "pass"}),
            patch("scripts.verify_production_deployment.check_loopback_metrics", return_value={"name": "loopback_metrics", "status": "pass"}),
            patch(
                "scripts.verify_production_deployment.check_evidence_output_directory",
                return_value={"name": "filesystem_private_evidence", "status": "pass"},
            ),
            patch("scripts.verify_production_deployment.write_evidence", side_effect=OSError("disk unavailable")),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 1)

        evidence = json.loads(stdout.getvalue())
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["checks"][-1]["name"], "evidence_output")

    def test_evidence_pre_write_provenance_stays_bound_to_deployment_root(self) -> None:
        stdout = io.StringIO()
        deployment_root = Path("deployed-root")
        clean = {
            "project_version": "1.0.11",
            "git_revision": TEST_SOURCE_REVISION,
            "git_revision_status": "ok",
            "git_worktree_status": "clean",
        }
        wrong_checkout = {
            "project_version": "1.0.11",
            "git_revision": "c" * 40,
            "git_revision_status": "ok",
            "git_worktree_status": "clean",
        }

        def identify(root: Path = deployment.PROJECT_ROOT) -> dict[str, str]:
            return clean if root == deployment_root else wrong_checkout

        with (
            patch.object(
                sys,
                "argv",
                [
                    "verify_production_deployment.py",
                    "--skip-systemd",
                    "--expected-version",
                    "1.0.11",
                    "--expected-source-revision",
                    TEST_SOURCE_REVISION,
                    "--expected-frontend-sha256",
                    TEST_FRONTEND_SHA256,
                    "--deployment-root",
                    str(deployment_root),
                    "--output",
                    "deployment.json",
                ],
            ),
            patch(
                "scripts.verify_production_deployment.source_identity",
                side_effect=identify,
            ) as identity,
            patch(
                "scripts.verify_production_deployment.check_loopback",
                return_value={"name": "loopback_health", "status": "pass"},
            ),
            patch(
                "scripts.verify_production_deployment.check_loopback_metrics",
                return_value={"name": "loopback_metrics", "status": "pass"},
            ),
            patch(
                "scripts.verify_production_deployment.check_evidence_output_directory",
                return_value={"name": "evidence_output_directory", "status": "pass"},
            ),
            patch("scripts.verify_production_deployment.write_evidence") as write,
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(identity.call_args_list, [call(deployment_root)] * 3)
        evidence = json.loads(stdout.getvalue())
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["source"], clean)
        pre_write = next(check for check in evidence["checks"] if check["name"] == "source_revision_pre_write")
        self.assertEqual(pre_write["status"], "pass")
        write.assert_called_once()

    def test_verifier_requires_an_expected_release_version(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["verify_production_deployment.py", "--skip-systemd"]),
            patch("scripts.verify_production_deployment.check_loopback") as check_loopback_mock,
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 1)

        evidence = json.loads(stdout.getvalue())
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["checks"][0]["name"], "expected_version")
        self.assertIn("--expected-version is required", evidence["checks"][0]["detail"])
        check_loopback_mock.assert_not_called()

    def test_skip_systemd_runs_loopback_checks_without_linux_host_assumptions(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "verify_production_deployment.py",
                    "--skip-systemd",
                    "--expected-version",
                    "1.0.11",
                    "--expected-source-revision",
                    TEST_SOURCE_REVISION,
                    "--expected-frontend-sha256",
                    TEST_FRONTEND_SHA256,
                ],
            ),
            patch("scripts.verify_production_deployment.check_source_revision", return_value={"name": "source_revision", "status": "pass"}) as source_revision_check,
            patch("scripts.verify_production_deployment.check_systemd") as systemd_check,
            patch("scripts.verify_production_deployment.check_filesystem_permissions") as filesystem_check,
            patch("scripts.verify_production_deployment.check_loopback", return_value={"name": "loopback_health", "status": "pass"}),
            patch("scripts.verify_production_deployment.check_loopback_metrics", return_value={"name": "loopback_metrics", "status": "pass"}),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 0)

        evidence = json.loads(stdout.getvalue())
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(source_revision_check.call_count, 2)
        self.assertTrue(all(call.args[0] == TEST_SOURCE_REVISION for call in source_revision_check.call_args_list))
        self.assertTrue(all(len(call.args) == 2 for call in source_revision_check.call_args_list))
        systemd_check.assert_not_called()
        filesystem_check.assert_not_called()

    def test_host_verifier_checks_the_explicit_backup_directory(self) -> None:
        stdout = io.StringIO()
        backup_directory = Path("trusted-backups")
        source = {
            "project_version": "1.0.11",
            "git_revision": TEST_SOURCE_REVISION,
            "git_revision_status": "ok",
            "git_worktree_status": "clean",
        }
        with (
            patch.object(
                sys,
                "argv",
                [
                    "verify_production_deployment.py",
                    "--expected-version",
                    "1.0.11",
                    "--expected-source-revision",
                    TEST_SOURCE_REVISION,
                    "--expected-frontend-sha256",
                    TEST_FRONTEND_SHA256,
                    "--backup-directory",
                    str(backup_directory),
                    "--evidence-run-id",
                    "7",
                    "--evidence-run-attempt",
                    "1",
                    "--evidence-nonce",
                    f"{TEST_SOURCE_REVISION}:7:1",
                ],
            ),
            patch("scripts.verify_production_deployment.source_identity", return_value=source),
            patch(
                "scripts.verify_production_deployment.check_source_revision",
                return_value={"name": "source_revision", "status": "pass"},
            ),
            patch("scripts.verify_production_deployment.check_systemd", return_value=[]) as systemd_check,
            patch(
                "scripts.verify_production_deployment.check_filesystem_permissions",
                return_value=[],
            ) as filesystem_check,
            patch(
                "scripts.verify_production_deployment.check_backup_evidence",
                return_value={"name": "verified_recent_state_backup", "status": "pass"},
            ) as backup_check,
            patch(
                "scripts.verify_production_deployment.check_durable_state_wiring",
                return_value={"name": "durable_state_wiring", "status": "pass"},
            ) as durable_state_check,
            patch(
                "scripts.verify_production_deployment.check_loopback",
                return_value={"name": "loopback_health", "status": "pass"},
            ),
            patch(
                "scripts.verify_production_deployment.check_loopback_metrics",
                return_value={"name": "loopback_metrics", "status": "pass"},
            ),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(json.loads(stdout.getvalue())["status"], "ok")
        systemd_check.assert_called_once_with()
        filesystem_check.assert_called_once_with(backup_directory=backup_directory)
        durable_state_check.assert_called_once_with()
        backup_check.assert_called_once_with(backup_directory)

    def test_verifier_requires_an_expected_source_revision(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["verify_production_deployment.py", "--skip-systemd", "--expected-version", "1.0.11"],
            ),
            patch("scripts.verify_production_deployment.check_loopback") as check_loopback_mock,
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 1)

        evidence = json.loads(stdout.getvalue())
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["checks"][0]["name"], "expected_source_revision")
        self.assertIn("--expected-source-revision is required", evidence["checks"][0]["detail"])
        check_loopback_mock.assert_not_called()

    def test_public_verifier_rejects_an_empty_upstream_token_before_network_checks(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "verify_production_deployment.py",
                    "--skip-systemd",
                    "--expected-version",
                    "1.0.11",
                    "--expected-source-revision",
                    TEST_SOURCE_REVISION,
                    "--expected-frontend-sha256",
                    TEST_FRONTEND_SHA256,
                    "--public-url",
                    "https://analytics.example.com",
                    "--public-basic-user",
                    "operator",
                ],
            ),
            patch(
                "scripts.verify_production_deployment.check_source_revision",
                return_value={"name": "source_revision", "status": "pass"},
            ),
            patch.dict(os.environ, {"MARKET_SENTINEL_API_TOKEN": ""}),
            patch("scripts.verify_production_deployment.check_loopback") as check_loopback_mock,
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 1)

        evidence = json.loads(stdout.getvalue())
        self.assertEqual(evidence["status"], "failed")
        self.assertTrue(
            any("non-empty upstream API token" in check.get("detail", "") for check in evidence["checks"])
        )
        check_loopback_mock.assert_not_called()

    def test_verifier_cannot_record_ok_when_source_changes_during_collection(self) -> None:
        clean = {
            "project_version": "1.0.11",
            "git_revision": TEST_SOURCE_REVISION,
            "git_revision_status": "ok",
            "git_worktree_status": "clean",
        }
        changed = {
            "project_version": "1.0.11",
            "git_revision": "b" * 40,
            "git_revision_status": "ok",
            "git_worktree_status": "dirty",
        }
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "verify_production_deployment.py",
                    "--skip-systemd",
                    "--expected-version",
                    "1.0.11",
                    "--expected-source-revision",
                    TEST_SOURCE_REVISION,
                    "--expected-frontend-sha256",
                    TEST_FRONTEND_SHA256,
                ],
            ),
            patch("scripts.verify_production_deployment.source_identity", side_effect=[clean, changed]),
            patch(
                "scripts.verify_production_deployment.check_loopback",
                return_value={"name": "loopback_health", "status": "pass"},
            ),
            patch(
                "scripts.verify_production_deployment.check_loopback_metrics",
                return_value={"name": "loopback_metrics", "status": "pass"},
            ),
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 1)

        evidence = json.loads(stdout.getvalue())
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["source"], changed)
        final = next(check for check in evidence["checks"] if check["name"] == "source_revision_final")
        self.assertEqual(final["status"], "fail")

    def test_public_verifier_proves_loopback_token_enforcement_and_proxy_routes(self) -> None:
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "verify_production_deployment.py",
                    "--skip-systemd",
                    "--expected-version",
                    "1.0.11",
                    "--expected-source-revision",
                    TEST_SOURCE_REVISION,
                    "--expected-frontend-sha256",
                    TEST_FRONTEND_SHA256,
                    "--public-url",
                    "https://analytics.example.com",
                    "--public-basic-user",
                    "operator",
                    "--public-basic-password-env",
                    "TEST_PUBLIC_PASSWORD",
                ],
            ),
            patch.dict(
                os.environ,
                {
                    "MARKET_SENTINEL_API_TOKEN": "api-token",
                    "TEST_PUBLIC_PASSWORD": "secret",
                },
            ),
            patch(
                "scripts.verify_production_deployment.check_source_revision",
                return_value={"name": "source_revision", "status": "pass"},
            ),
            patch(
                "scripts.verify_production_deployment.check_loopback",
                return_value={"name": "loopback_health", "status": "pass"},
            ),
            patch(
                "scripts.verify_production_deployment.check_loopback_metrics",
                return_value={"name": "loopback_metrics", "status": "pass"},
            ),
            patch(
                "scripts.verify_production_deployment.check_loopback_token_auth",
                return_value={"name": "loopback_token_auth", "status": "pass"},
            ) as token_auth,
            patch(
                "scripts.verify_production_deployment.check_public_proxy",
                return_value={"name": "public_https_proxy", "status": "pass"},
            ) as public_proxy,
            contextlib.redirect_stdout(stdout),
        ):
            self.assertEqual(main(), 0)

        evidence = json.loads(stdout.getvalue())
        self.assertEqual(evidence["status"], "ok")
        token_auth.assert_called_once_with("http://127.0.0.1:8765/api/health", 10.0)
        public_proxy.assert_called_once_with(
            "https://analytics.example.com",
            "operator",
            "secret",
            10.0,
            "1.0.11",
            "api-token",
            TEST_SOURCE_REVISION,
            TEST_FRONTEND_SHA256,
        )

    def test_public_proxy_requires_https_security_headers_and_no_store(self) -> None:
        headers = {
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Content-Security-Policy": "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; connect-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "no-store",
        }
        with patch(
            "scripts.verify_production_deployment.urlopen",
            side_effect=[
                *_unauthorized_errors(),
                _Response(headers, {"status": "ok", "api_version": "1.0.10"}),
            ],
        ):
            self.assertEqual(
                check_public_proxy(
                    "https://analytics.example.com",
                    "operator",
                    "secret",
                    1.0,
                    "1.0.10",
                    "api-token",
                )["status"],
                "pass",
            )
        weak_headers = {**headers, "X-Frame-Options": "SAMEORIGIN"}
        with patch(
            "scripts.verify_production_deployment.urlopen",
            side_effect=[
                *_unauthorized_errors(),
                _Response(weak_headers, {"status": "ok", "api_version": "1.0.10"}),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "incomplete security-header policy: x-frame-options"):
                check_public_proxy(
                    "https://analytics.example.com",
                    "operator",
                    "secret",
                    1.0,
                    upstream_token="api-token",
                )
        server_headers = {**headers, "Server": "Caddy"}
        with patch(
            "scripts.verify_production_deployment.urlopen",
            side_effect=[
                *_unauthorized_errors(),
                _Response(server_headers, {"status": "ok", "api_version": "1.0.10"}),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "exposes a Server header"):
                check_public_proxy(
                    "https://analytics.example.com",
                    "operator",
                    "secret",
                    1.0,
                    upstream_token="api-token",
                )
        with self.assertRaisesRegex(ValueError, "Basic Auth credentials"):
            check_public_proxy("https://analytics.example.com", "", "secret", 1.0, upstream_token="api-token")
        with self.assertRaisesRegex(ValueError, "upstream API token"):
            check_public_proxy("https://analytics.example.com", "operator", "secret", 1.0)
        with patch("scripts.verify_production_deployment.urlopen", return_value=_Response(headers, {"status": "ok", "api_version": "1.0.10"})):
            with self.assertRaisesRegex(RuntimeError, "unauthenticated public proxy GET / was accepted"):
                check_public_proxy(
                    "https://analytics.example.com",
                    "operator",
                    "secret",
                    1.0,
                    upstream_token="api-token",
                )
        with patch(
            "scripts.verify_production_deployment.urlopen",
            side_effect=HTTPError("https://analytics.example.com/api/health", 403, "Forbidden", {}, None),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403, expected 401"):
                check_public_proxy(
                    "https://analytics.example.com",
                    "operator",
                    "secret",
                    1.0,
                    upstream_token="api-token",
                )
        with patch(
            "scripts.verify_production_deployment.urlopen",
            side_effect=[
                *_unauthorized_errors(),
                _Response(headers, {"status": "ok", "api_version": "1.0.10"}),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "expected 1.0.11"):
                check_public_proxy(
                    "https://analytics.example.com",
                    "operator",
                    "secret",
                    1.0,
                    "1.0.11",
                    "api-token",
                )
        with self.assertRaisesRegex(ValueError, "absolute https"):
            check_public_proxy("http://analytics.example.com", "", "", 1.0)

    def test_public_proxy_checks_static_read_metrics_state_and_mutation_routes(self) -> None:
        headers = {
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Content-Security-Policy": "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; connect-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "no-store",
        }
        calls: list[tuple[str, str, bytes | None]] = []
        errors = _unauthorized_errors()

        def urlopen(request, timeout):
            calls.append((request.get_method(), request.full_url, request.data))
            if errors:
                raise errors.pop(0)
            return _Response(headers, {"status": "ok", "api_version": "1.0.10"})

        with patch("scripts.verify_production_deployment.urlopen", side_effect=urlopen):
            result = check_public_proxy(
                "https://analytics.example.com",
                "operator",
                "secret",
                1.0,
                "1.0.10",
                "api-token",
            )

        self.assertEqual(result["unauthenticated_probes"], len(PUBLIC_PROXY_AUTH_PROBES))
        self.assertEqual(
            [(method, url) for method, url, _ in calls[:-1]],
            [
                ("GET", "https://analytics.example.com/"),
                ("GET", "https://analytics.example.com/api/health"),
                ("GET", "https://analytics.example.com/api/state"),
                ("GET", "https://analytics.example.com/metrics"),
                ("PATCH", "https://analytics.example.com/api/config"),
            ],
        )
        self.assertEqual(calls[-2][2], b"{")
        self.assertEqual(calls[-1][:2], ("GET", "https://analytics.example.com/api/health"))

    def test_public_proxy_closes_the_unauthenticated_error_response(self) -> None:
        headers = {
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Content-Security-Policy": "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; connect-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "no-store",
        }
        bodies = [io.BytesIO(b'{"error":"unauthorized"}') for _ in PUBLIC_PROXY_AUTH_PROBES]
        with patch(
            "scripts.verify_production_deployment.urlopen",
            side_effect=[
                *_unauthorized_errors(bodies=bodies),
                _Response(headers, {"status": "ok", "api_version": "1.0.10"}),
            ],
        ):
            self.assertEqual(
                check_public_proxy(
                    "https://analytics.example.com",
                    "operator",
                    "secret",
                    1.0,
                    "1.0.10",
                    "api-token",
                )["status"],
                "pass",
            )
        self.assertTrue(all(body.closed for body in bodies))

    def test_loopback_token_auth_requires_a_401_and_closes_the_error(self) -> None:
        body = io.BytesIO(b'{"error":"unauthorized"}')
        unauthorized = HTTPError("http://127.0.0.1:8765/api/health", 401, "Unauthorized", {}, body)
        with patch("scripts.verify_production_deployment.urlopen", side_effect=unauthorized):
            self.assertEqual(
                check_loopback_token_auth("http://127.0.0.1:8765/api/health", 1.0)["status"],
                "pass",
            )
        self.assertTrue(body.closed)

        with patch(
            "scripts.verify_production_deployment.urlopen",
            return_value=_Response({}, {"status": "ok", "api_version": "1.0.10"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "loopback API request was accepted"):
                check_loopback_token_auth("http://127.0.0.1:8765/api/health", 1.0)


    def test_authenticated_public_redirect_is_rejected_before_credentials_can_move(self) -> None:
        request = Request(
            "https://markets.example.net/api/health",
            headers={"Authorization": "Basic secret"},
        )
        with self.assertRaisesRegex(RuntimeError, "redirects are forbidden"):
            _RejectRedirects().redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://attacker.example/api/health",
            )

    def test_private_loopback_and_link_local_public_origins_are_rejected(self) -> None:
        for origin in ("https://127.0.0.1", "https://10.0.0.4", "https://169.254.1.2", "https://[::1]"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                _validated_public_origin(origin)


if __name__ == "__main__":
    unittest.main()
