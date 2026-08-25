from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.deployment_identity import (
    FRONTEND_SHA256_ENV,
    SOURCE_REVISION_ENV,
    capture_runtime_identity,
    frontend_tree_sha256,
    git_source_revision,
    safe_git_command,
)


class DeploymentIdentityTests(unittest.TestCase):
    def test_safe_git_command_scopes_trust_to_the_resolved_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            command = safe_git_command(root, "status", "--porcelain=v1")

        self.assertEqual(
            command,
            [
                "git",
                "-c",
                f"safe.directory={root.resolve()}",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
            ],
        )

    def test_frontend_digest_binds_relative_paths_and_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            (root / "index.html").write_text("index", encoding="utf-8")
            asset = root / "assets" / "app.js"
            asset.write_text("one", encoding="utf-8")
            initial = frontend_tree_sha256(root)

            asset.write_text("two", encoding="utf-8")
            changed_content = frontend_tree_sha256(root)
            asset.rename(root / "assets" / "renamed.js")
            changed_path = frontend_tree_sha256(root)

        self.assertRegex(initial, r"^[0-9a-f]{64}$")
        self.assertNotEqual(initial, changed_content)
        self.assertNotEqual(changed_content, changed_path)

    def test_git_source_revision_scrubs_git_environment_and_binds_the_root(self) -> None:
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.dict(
                os.environ,
                {"PATH": os.environ.get("PATH", ""), "GIT_DIR": "attacker", "git_work_tree": "shadow"},
                clear=True,
            ):
                with patch(
                    "core.deployment_identity.subprocess.run",
                    side_effect=[
                        subprocess.CompletedProcess(["git"], 0, f"{root}\n", ""),
                        subprocess.CompletedProcess(["git"], 0, revision + "\n", ""),
                    ],
                ) as runner:
                    actual = git_source_revision(root)

            self.assertEqual(actual, revision)
            self.assertEqual(runner.call_count, 2)
            for call in runner.call_args_list:
                environment = call.kwargs["env"]
                self.assertFalse(any(name.upper().startswith("GIT_") for name in environment))
                self.assertEqual(environment["LC_ALL"], "C")
                self.assertEqual(environment["TZ"], "UTC")
                self.assertEqual(call.kwargs["cwd"], root)

            other = root / "other"
            other.mkdir()
            with patch(
                "core.deployment_identity.subprocess.run",
                return_value=subprocess.CompletedProcess(["git"], 0, f"{other}\n", ""),
            ) as runner:
                self.assertEqual(git_source_revision(root), "")
            self.assertEqual(runner.call_count, 1)

    def test_runtime_identity_rejects_checkout_and_frontend_mismatches(self) -> None:
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "frontend" / "dist"
            frontend.mkdir(parents=True)
            (frontend / "index.html").write_text("reviewed", encoding="utf-8")
            digest = frontend_tree_sha256(frontend)

            with patch("core.deployment_identity.git_source_revision", return_value=revision):
                identity = capture_runtime_identity(
                    root,
                    frontend,
                    {SOURCE_REVISION_ENV: revision, FRONTEND_SHA256_ENV: digest},
                )
                with self.assertRaisesRegex(ValueError, SOURCE_REVISION_ENV):
                    capture_runtime_identity(
                        root,
                        frontend,
                        {SOURCE_REVISION_ENV: "b" * 40, FRONTEND_SHA256_ENV: digest},
                    )

            (frontend / "index.html").write_text("tampered", encoding="utf-8")
            with patch("core.deployment_identity.git_source_revision", return_value=revision):
                with self.assertRaisesRegex(ValueError, FRONTEND_SHA256_ENV):
                    capture_runtime_identity(
                        root,
                        frontend,
                        {SOURCE_REVISION_ENV: revision, FRONTEND_SHA256_ENV: digest},
                    )

        self.assertEqual(
            identity,
            {"source_revision": revision, "frontend_sha256": digest},
        )

    def test_runtime_identity_rejects_invalid_configured_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "frontend"
            frontend.mkdir()
            with self.assertRaisesRegex(ValueError, SOURCE_REVISION_ENV):
                capture_runtime_identity(root, frontend, {SOURCE_REVISION_ENV: "not-a-commit"})
            with self.assertRaisesRegex(ValueError, FRONTEND_SHA256_ENV):
                capture_runtime_identity(root, frontend, {FRONTEND_SHA256_ENV: "not-a-digest"})


if __name__ == "__main__":
    unittest.main()
