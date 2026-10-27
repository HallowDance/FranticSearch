"""Off-machine backups (PLAN.md § Off-machine backups; docs/SCALING.md O1, S3).

``push()`` encrypts a backup with gpg to a public key and copies it to ``host:dir``
over SSH (rsync), or into a local directory (e.g. a USB disk), then keeps only the
newest ``keep`` copies there. The private key never needs to be on this machine:
gpg runs with a throwaway keyring that only ever holds the public key, so the
destination only sees ciphertext. Restoring: decrypt with the private key
(``gpg -d FILE.gpg > FILE``), then ``python -m franticsearch restore FILE``.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from .backup import USER_PREFIX

log = logging.getLogger(__name__)

KEEP = 30
SUFFIX = ".gpg"
_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]


class OffsiteError(RuntimeError):
    pass


def _run(cmd: list[str], what: str) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise OffsiteError(f"{what}: {cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise OffsiteError(f"{what}: timed out") from None
    if r.returncode != 0:
        raise OffsiteError(f"{what} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout


def split_dest(dest: str) -> tuple[str | None, str]:
    """``host:dir`` → (host, dir); a plain path (no colon before the first slash) is local."""
    head, sep, tail = dest.partition(":")
    if sep and "/" not in head and head:
        return head, tail or "."
    return None, dest


def encrypt(source: Path, key_file: Path, out_dir: Path) -> Path:
    """``source`` encrypted to the public key(s) in ``key_file``, as ``out_dir/<name>.gpg``."""
    if not key_file.is_file():
        raise OffsiteError(f"no public key at {key_file} (see PLAN.md § Off-machine backups)")
    target = out_dir / (source.name + SUFFIX)
    with tempfile.TemporaryDirectory(prefix="fs-gpg-") as home:
        _run(["gpg", "--homedir", home, "--batch", "--yes", "--no-tty", "--quiet", "--trust-model", "always",
              "--recipient-file", str(key_file), "--output", str(target), "--encrypt", str(source)],
             "gpg encrypt")
    return target


def _prune_local(d: Path, keep: int) -> list[str]:
    old = sorted(d.glob(f"{USER_PREFIX}*{SUFFIX}"), key=lambda p: p.name, reverse=True)[keep:]
    for p in old:
        p.unlink()
    return [p.name for p in old]


def push(source: str | Path, key_file: str | Path, dest: str, keep: int = KEEP) -> dict:
    """Encrypt ``source`` and copy it to ``dest`` (``host:dir`` or a local directory), then
    keep the newest ``keep`` encrypted backups there. Returns {"file", "dest", "removed"}."""
    if keep < 1:
        raise OffsiteError("keep must be at least 1")
    source, key_file = Path(source), Path(key_file).expanduser()
    host, directory = split_dest(dest)
    with tempfile.TemporaryDirectory(prefix="fs-offsite-") as tmp:
        enc = encrypt(source, key_file, Path(tmp))
        if host is None:
            d = Path(directory).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            part = d / (enc.name + ".part")
            shutil.copy2(enc, part)
            part.chmod(0o600)
            part.rename(d / enc.name)
            removed = _prune_local(d, keep)
        else:
            q = shlex.quote(directory)
            _run(_SSH + [host, f"mkdir -p {q} && chmod 700 {q}"], "ssh mkdir")
            _run(["rsync", "-t", "--chmod=F600", "--partial-dir=.rsync-partial", "-e", " ".join(_SSH), str(enc),
                  f"{host}:{directory.rstrip('/')}/"], "rsync")
            out = _run(_SSH + [host, f"cd {q} && old=$(ls -1 {USER_PREFIX}*{SUFFIX} 2>/dev/null | sort -r"
                               f" | tail -n +{keep + 1}) && printf '%s\\n' \"$old\" | xargs -r rm -- "
                               "&& printf '%s\\n' \"$old\""], "ssh prune")
            removed = out.split()
    log.info("off-machine backup: %s → %s (%d old removed)", enc.name, dest, len(removed))
    return {"file": enc.name, "dest": dest, "removed": removed}
