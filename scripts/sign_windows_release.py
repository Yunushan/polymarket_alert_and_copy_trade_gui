from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_TIMESTAMP_URL = "https://timestamp.digicert.com"
_SIGNING_SECRET_ENVIRONMENT_KEYS = (
    "WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64",
    "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD",
)


_SIGN_AND_VERIFY_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$certificateStore = "Cert:\CurrentUser\My"
$importAttempted = $false
$thumbprint = $null
$securePassword = $null
$pfxCertificates = $null
$pfxThumbprints = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)
$preexistingThumbprints = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
)

function Get-NormalizedThumbprint {
    param([System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate)

    $candidate = ([string]$Certificate.Thumbprint).Replace(" ", "").ToUpperInvariant()
    if ($candidate -notmatch "^[0-9A-F]{40}$") {
        throw "The signing certificate thumbprint is invalid."
    }
    return $candidate
}

try {
    $pfxPath = $env:MARKET_SENTINEL_SIGNING_PFX_PATH
    $password = $env:WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD
    $signTool = $env:MARKET_SENTINEL_SIGNTOOL_PATH
    $timestampUrl = $env:MARKET_SENTINEL_SIGNING_TIMESTAMP_URL
    $targets = @(ConvertFrom-Json -InputObject $env:MARKET_SENTINEL_SIGNING_TARGETS_JSON)

    if ([string]::IsNullOrWhiteSpace($pfxPath) -or [string]::IsNullOrEmpty($password)) {
        throw "The signing certificate environment is incomplete."
    }

    # Enumerate the complete PFX in memory first. Import-PfxCertificate can
    # import certificate chains and can return an array, so a scalar probe is
    # not sufficient either for signer selection or scoped cleanup.
    $pfxCertificates = [System.Security.Cryptography.X509Certificates.X509Certificate2Collection]::new()
    $pfxCertificates.Import(
        $pfxPath,
        $password,
        [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet
    )
    $signingCandidates = @()
    foreach ($certificate in $pfxCertificates) {
        $candidateThumbprint = Get-NormalizedThumbprint $certificate
        [void]$pfxThumbprints.Add($candidateThumbprint)
        if ($certificate.HasPrivateKey) {
            $signingCandidates += $certificate
        }
    }
    if ($signingCandidates.Count -ne 1) {
        throw "The signing PFX must contain exactly one certificate with a private key."
    }
    $thumbprint = Get-NormalizedThumbprint $signingCandidates[0]

    foreach ($certificate in @(Get-ChildItem -LiteralPath $certificateStore -ErrorAction Stop)) {
        [void]$preexistingThumbprints.Add((Get-NormalizedThumbprint $certificate))
    }

    $certificatePath = "$certificateStore\$thumbprint"
    $existingCertificate = Get-Item -LiteralPath $certificatePath -ErrorAction SilentlyContinue
    if ($null -ne $existingCertificate) {
        if (-not $existingCertificate.HasPrivateKey) {
            throw "A certificate with this thumbprint exists without its private key."
        }
    } else {
        $securePassword = ConvertTo-SecureString -String $password -AsPlainText -Force
        $importAttempted = $true
        $importedCertificates = @(Import-PfxCertificate `
            -FilePath $pfxPath `
            -CertStoreLocation $certificateStore `
            -Password $securePassword `
            -Exportable:$false)
        $matchingImportedCertificates = @(
            foreach ($certificate in $importedCertificates) {
                if (
                    (Get-NormalizedThumbprint $certificate) -ceq $thumbprint -and
                    $certificate.HasPrivateKey
                ) {
                    $certificate
                }
            }
        )
        if ($matchingImportedCertificates.Count -ne 1) {
            throw "The signing certificate import did not provide a private key."
        }
        $installedCertificate = Get-Item -LiteralPath $certificatePath -ErrorAction SilentlyContinue
        if ($null -eq $installedCertificate -or -not $installedCertificate.HasPrivateKey) {
            throw "The imported signing certificate is unavailable in the current-user store."
        }
    }

    # The signing tools need only the installed certificate. Do not pass the PFX
    # password (or path) to their environment, much less their command line.
    Remove-Item Env:\WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\MARKET_SENTINEL_SIGNING_PFX_PATH -ErrorAction SilentlyContinue
    $password = $null
    if ($null -ne $securePassword) {
        $securePassword.Dispose()
        $securePassword = $null
    }

    foreach ($target in $targets) {
        & $signTool sign /fd SHA256 /sha1 $thumbprint /s My /tr $timestampUrl /td SHA256 ([string]$target)
        if ($LASTEXITCODE -ne 0) {
            throw "Authenticode signing failed."
        }
        & $signTool verify /pa /all ([string]$target)
        if ($LASTEXITCODE -ne 0) {
            throw "Authenticode verification failed."
        }
    }
} finally {
    $cleanupFailed = $false
    Remove-Item Env:\WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:\MARKET_SENTINEL_SIGNING_PFX_PATH -ErrorAction SilentlyContinue
    $password = $null
    if ($null -ne $securePassword) {
        try {
            $securePassword.Dispose()
        } catch {
            $cleanupFailed = $true
        }
        $securePassword = $null
    }
    if ($importAttempted) {
        foreach ($candidateThumbprint in $pfxThumbprints) {
            if (-not $preexistingThumbprints.Contains($candidateThumbprint)) {
                $candidatePath = "$certificateStore\$candidateThumbprint"
                try {
                    if (Test-Path -LiteralPath $candidatePath) {
                        Remove-Item -LiteralPath $candidatePath -Force -ErrorAction Stop
                    }
                } catch {
                    if (Test-Path -LiteralPath $candidatePath) {
                        $cleanupFailed = $true
                    }
                }
            }
        }
    }
    foreach ($certificate in @($pfxCertificates)) {
        try {
            $certificate.Dispose()
        } catch {
            $cleanupFailed = $true
        }
    }
    if ($cleanupFailed) {
        throw "Signing certificate cleanup failed."
    }
}
"""


def signtool_path() -> str:
    executable = shutil.which("signtool.exe") or shutil.which("signtool")
    if not executable:
        raise SystemExit("signtool was not found. Install the Windows SDK signing tools on the release runner.")
    return executable


def powershell_path() -> str:
    executable = shutil.which("pwsh.exe") or shutil.which("pwsh")
    executable = executable or shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        raise SystemExit("PowerShell was not found. It is required to import the Windows signing certificate.")
    return executable


def decode_certificate(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SystemExit("WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64 is not valid base64.") from exc


def normalize_timestamp_url(value: str | None) -> str:
    timestamp_url = str(value or "").strip() or DEFAULT_TIMESTAMP_URL
    parsed = urlsplit(timestamp_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise SystemExit("Windows signing timestamp URL must be an absolute HTTPS URL without credentials or a fragment.")
    return timestamp_url


def sign_files(
    executable: str,
    certificate_path: Path,
    password: str,
    timestamp_url: str,
    targets: list[Path],
) -> None:
    timestamp_url = normalize_timestamp_url(timestamp_url)
    environment = os.environ.copy()
    for key in _SIGNING_SECRET_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment.update(
        {
            "WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD": password,
            "MARKET_SENTINEL_SIGNING_PFX_PATH": str(certificate_path),
            "MARKET_SENTINEL_SIGNTOOL_PATH": executable,
            "MARKET_SENTINEL_SIGNING_TIMESTAMP_URL": timestamp_url,
            "MARKET_SENTINEL_SIGNING_TARGETS_JSON": json.dumps([str(target) for target in targets]),
        }
    )
    command = [
        powershell_path(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _SIGN_AND_VERIFY_SCRIPT,
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, env=environment)
    except OSError:
        raise SystemExit("Windows signing could not start PowerShell.") from None
    if result.returncode != 0:
        # PowerShell and signtool output is deliberately not reflected into the
        # exception: provider errors must never echo secret-bearing input.
        raise SystemExit("Windows signing or certificate cleanup failed; no provider output was exposed.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign and verify Windows MarketSentinel release files.")
    parser.add_argument("--path", action="append", required=True, type=Path, help="EXE or MSI file to sign.")
    parser.add_argument(
        "--timestamp-url",
        default=os.environ.get("WINDOWS_CODE_SIGNING_TIMESTAMP_URL"),
    )
    args = parser.parse_args()
    timestamp_url = normalize_timestamp_url(args.timestamp_url)
    certificate = os.environ.get("WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64", "").strip()
    password = os.environ.get("WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD", "")
    if not certificate or not password:
        raise SystemExit(
            "WINDOWS_CODE_SIGNING_CERTIFICATE_BASE64 and WINDOWS_CODE_SIGNING_CERTIFICATE_PASSWORD are required."
        )
    targets = [path.resolve() for path in args.path]
    missing = [str(path) for path in targets if not path.is_file()]
    if missing:
        raise SystemExit("Signing targets do not exist: " + ", ".join(missing))

    executable = signtool_path()
    with tempfile.TemporaryDirectory(prefix="market-sentinel-sign-") as tmpdir:
        certificate_path = Path(tmpdir) / "release-signing.pfx"
        certificate_path.write_bytes(decode_certificate(certificate))
        sign_files(executable, certificate_path, password, timestamp_url, targets)
        for target in targets:
            print(f"[ok] signed {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
