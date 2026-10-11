from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT_DIR

logger = logging.getLogger(__name__)

CERT_DIR = ROOT_DIR / ".certs"
WINDOWS_ROOTS_PEM = CERT_DIR / "windows-roots.pem"


def _export_windows_roots(dest: Path) -> bool:
    """Export LocalMachine/CurrentUser Root (+ CA) certs to a PEM for Node."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ps = r"""
$ErrorActionPreference = 'Stop'
$out = $env:PAB_CERT_OUT
$sb = New-Object System.Text.StringBuilder
function Export-Store([string]$name, [string]$location) {
  $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($name, $location)
  try {
    $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
    foreach ($cert in $store.Certificates) {
      try {
        $b64 = [Convert]::ToBase64String($cert.RawData, [System.Base64FormattingOptions]::InsertLineBreaks)
        [void]$sb.AppendLine('-----BEGIN CERTIFICATE-----')
        [void]$sb.AppendLine($b64)
        [void]$sb.AppendLine('-----END CERTIFICATE-----')
      } catch {}
    }
  } finally {
    $store.Close()
  }
}
Export-Store 'Root' 'LocalMachine'
Export-Store 'Root' 'CurrentUser'
Export-Store 'CA' 'LocalMachine'
Export-Store 'CA' 'CurrentUser'
[System.IO.File]::WriteAllText($out, $sb.ToString())
"""
    env = os.environ.copy()
    env["PAB_CERT_OUT"] = str(dest)
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            check=True,
            env=env,
            creationflags=flags,
            timeout=60,
        )
    except Exception:
        logger.exception("Failed to export Windows CA bundle for Node")
        return False
    return dest.is_file() and dest.stat().st_size > 0


def prepare_node_tls_env() -> None:
    """
    Bundled cursor-sdk node.exe does not use the Windows cert store.
    Kaspersky HTTPS inspection then breaks CreateAgent as
    'internal: Network request failed'. Point Node at exported roots.
    """
    if sys.platform != "win32":
        return

    insecure = (os.getenv("CURSOR_BRIDGE_INSECURE_TLS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if insecure:
        os.environ["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        logger.warning(
            "CURSOR_BRIDGE_INSECURE_TLS enabled — Node TLS verification disabled"
        )
        return

    if os.environ.get("NODE_EXTRA_CA_CERTS"):
        logger.info(
            "Using existing NODE_EXTRA_CA_CERTS=%s", os.environ["NODE_EXTRA_CA_CERTS"]
        )
        return

    # Refresh once per process start (cheap enough; certs may change after AV install).
    if not _export_windows_roots(WINDOWS_ROOTS_PEM):
        logger.error(
            "Could not build Windows CA PEM. "
            "Allow network for node.exe in Kaspersky, or set "
            "CURSOR_BRIDGE_INSECURE_TLS=1 in env.env (less safe)."
        )
        return

    os.environ["NODE_EXTRA_CA_CERTS"] = str(WINDOWS_ROOTS_PEM)
    logger.info("NODE_EXTRA_CA_CERTS=%s", WINDOWS_ROOTS_PEM)
