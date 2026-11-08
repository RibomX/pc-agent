"""Check that a downloaded program is really signed by its publisher before the app runs it."""

import subprocess
from pathlib import Path

_verified: set[str] = set()


def signed_by(path: Path, organisation: str) -> bool:
    """True if Windows reports a valid Authenticode signature whose organisation matches."""
    key = f"{path}|{organisation}"
    if key in _verified:
        return True
    script = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{path}';"
        "if ($s.Status -eq 'Valid') { $s.SignerCertificate.Subject }"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                         timeout=30, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    ok = f"O={organisation}," in out or f'O="{organisation}",' in out
    if ok:
        _verified.add(key)
    return ok


def require(path: Path, organisation: str):
    """Delete the file and raise if it is not genuine – the next attempt downloads it again."""
    if not signed_by(path, organisation):
        path.unlink(missing_ok=True)
        raise RuntimeError(f"{path.name} is not signed by {organisation} – it was removed for safety.")
