from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.sign_windows_release import (
    DEFAULT_TIMESTAMP_URL,
    _SIGN_AND_VERIFY_SCRIPT,
    normalize_timestamp_url,
    sign_files,
)


class WindowsSigningTests(unittest.TestCase):
    def test_empty_timestamp_configuration_uses_reviewed_https_default(self) -> None:
        self.assertEqual(normalize_timestamp_url(None), DEFAULT_TIMESTAMP_URL)
        self.assertEqual(normalize_timestamp_url("   "), DEFAULT_TIMESTAMP_URL)

    def test_timestamp_url_is_trimmed_and_must_be_safe_https(self) -> None:
        self.assertEqual(
            normalize_timestamp_url("  https://timestamp.example.test/path  "),
            "https://timestamp.example.test/path",
        )
        for value in (
            "http://timestamp.example.test",
            "timestamp.example.test",
            "https://user:secret@timestamp.example.test",
            "https://timestamp.example.test/#fragment",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SystemExit, "absolute HTTPS URL"):
                    normalize_timestamp_url(value)

    def test_password_is_only_passed_to_powershell_through_environment(self) -> None:
        password = "never-put-this-password-in-argv"
        with tempfile.TemporaryDirectory() as tmpdir:
            certificate_path = Path(tmpdir) / "certificate.pfx"
            targets = [Path(tmpdir) / "application.exe", Path(tmpdir) / "installer.msi"]
            with (
                patch.dict(
                    os.environ,
                    {
                        "WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64": "encoded-secret",
                        "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD": "stale-secret",
                    },
                ),
                patch("scripts.sign_windows_release.powershell_path", return_value="pwsh.exe"),
                patch(
                    "scripts.sign_windows_release.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                ) as run,
            ):
                sign_files("signtool.exe", certificate_path, password, "https://timestamp.test", targets)

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertNotIn(password, command)
        self.assertNotIn("stale-secret", command)
        self.assertNotIn("/p", command)
        self.assertEqual(environment["WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD"], password)
        self.assertNotIn("WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64", environment)
        self.assertEqual(json.loads(environment["MARKET_SENTINEL_SIGNING_TARGETS_JSON"]), [str(p) for p in targets])
        self.assertIn("/sha1 $thumbprint", _SIGN_AND_VERIFY_SCRIPT)
        self.assertNotIn(" /p ", _SIGN_AND_VERIFY_SCRIPT)

    def test_failure_does_not_echo_password_or_child_output(self) -> None:
        password = "top-secret-pfx-password"
        failed = subprocess.CompletedProcess(
            [],
            1,
            stdout=f"provider accidentally printed {password}",
            stderr=f"signtool accidentally printed {password}",
        )
        with (
            patch("scripts.sign_windows_release.powershell_path", return_value="pwsh.exe"),
            patch("scripts.sign_windows_release.subprocess.run", return_value=failed),
            self.assertRaises(SystemExit) as caught,
        ):
            sign_files(
                "signtool.exe",
                Path("certificate.pfx"),
                password,
                "https://timestamp.test",
                [Path("application.exe")],
            )

        self.assertNotIn(password, str(caught.exception))
        self.assertNotIn("provider accidentally printed", str(caught.exception))

    def test_powershell_handles_multi_certificate_pfx_and_scopes_cleanup(self) -> None:
        self.assertIn("X509Certificate2Collection]::new()", _SIGN_AND_VERIFY_SCRIPT)
        self.assertIn("$importedCertificates = @(Import-PfxCertificate", _SIGN_AND_VERIFY_SCRIPT)
        self.assertIn("$signingCandidates.Count -ne 1", _SIGN_AND_VERIFY_SCRIPT)
        self.assertIn("$matchingImportedCertificates.Count -ne 1", _SIGN_AND_VERIFY_SCRIPT)
        self.assertIn("$preexistingThumbprints", _SIGN_AND_VERIFY_SCRIPT)
        self.assertIn("$pfxThumbprints", _SIGN_AND_VERIFY_SCRIPT)
        self.assertIn("if ($importAttempted)", _SIGN_AND_VERIFY_SCRIPT)
        self.assertIn("-not $preexistingThumbprints.Contains($candidateThumbprint)", _SIGN_AND_VERIFY_SCRIPT)
        self.assertIn("Remove-Item -LiteralPath $candidatePath", _SIGN_AND_VERIFY_SCRIPT)
        self.assertNotIn("$importedByInvocation", _SIGN_AND_VERIFY_SCRIPT)


if __name__ == "__main__":
    unittest.main()
