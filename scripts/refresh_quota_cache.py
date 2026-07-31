"""Refresh the on-disk quota cache used by the runtime footer.

Run on a schedule (cron) so the gateway footer can show per-provider quota
without doing network I/O on every message.  Exit 0 on success, non-zero on
fatal failure.  Prints nothing on success (watchdog pattern) so the cron
delivery stays silent; prints a short error line on failure so the cron
alerts.

Usage (cron script mode, no_agent=True):
    python scripts/refresh_quota_cache.py
"""

from __future__ import annotations

import os
import sys

# Run against the DEFAULT Hermes home (where the Telegram gateway lives),
# regardless of which profile the invoking shell happens to have active.
_DEFAULT_HOME = r"C:\Users\ronal\AppData\Local\hermes"
os.environ.setdefault("HERMES_HOME", _DEFAULT_HOME)

_HERE = os.path.dirname(os.path.abspath(__file__))
# The hermes-agent source tree lives one level up from scripts/ inside HERMES_HOME;
# also try the common install layout.
_CANDIDATES = [
    os.path.join(_DEFAULT_HOME, "hermes-agent"),
    os.path.join(_DEFAULT_HOME, "hermes-agent", "venv", "Lib", "site-packages"),
]
for _c in _CANDIDATES:
    if os.path.isdir(_c) and _c not in sys.path:
        sys.path.insert(0, _c)


def _reexec_with_venv() -> int:
    """Re-launch this script under the Hermes venv python if imports fail.

    The cron scheduler may invoke this .py with the system interpreter, which
    lacks ``agent`` / ``httpx``.  When the import below fails, find the bundled
    venv python and re-exec ourselves with it (preserving the cache path).
    Returns the re-exec exit code, or -1 if no venv python was found.
    """
    import subprocess

    for cand in (
        os.path.join(_DEFAULT_HOME, "hermes-agent", "venv", "Scripts", "python.exe"),
        os.path.join(_DEFAULT_HOME, "hermes-agent", "venv", "bin", "python"),
    ):
        if os.path.exists(cand):
            return subprocess.call([cand, os.path.abspath(__file__)])
    return -1


def main() -> int:
    try:
        from agent.quota_cache import refresh_quota_cache
    except Exception:
        # Likely running under the wrong interpreter — try the venv python.
        code = _reexec_with_venv()
        if code >= 0:
            return code
        print("quota refresh: cannot import agent.quota_cache (no venv python found)")
        return 1
    try:
        refresh_quota_cache(timeout=12.0)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"quota refresh: refresh failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
