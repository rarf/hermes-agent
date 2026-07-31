"""Extract grok.com / x.com session cookies from Chrome and save to ~/grok_session.json.

Reads the Chrome cookie SQLite (via browser_cookie3, which decrypts the DPAPI
store on Windows) for the domains grok.com and x.com, builds the Cookie header
string, and writes it to ~/grok_session.json in the format the Grok quota
fetcher / grok_probe.py expect.  Then optionally runs the probe.

NOTE: Chrome should be CLOSED (or at least not actively writing) when this runs,
otherwise the cookie DB may be locked.  The resulting file holds live session
secrets — keep it local, never commit or paste it.
"""

from __future__ import annotations

import json
import os
import sys

SESSION_PATH = os.path.join(os.path.expanduser("~"), "grok_session.json")


def _collect() -> str:
    import browser_cookie3

    cookies: dict[str, str] = {}

    def _grab(browser_fn, label):
        try:
            for c in browser_fn(domain_name="grok.com"):
                cookies[c.name] = c.value
        except Exception as e:  # pragma: no cover - env specific
            print(f"warn: grok.com cookies ({label}): {e}")
        try:
            for c in browser_fn(domain_name="x.com"):
                if c.name in ("sso", "sso-rw", "auth_token", "ct0", "x-userid", "twid"):
                    cookies.setdefault(c.name, c.value)
        except Exception as e:  # pragma: no cover
            print(f"warn: x.com cookies ({label}): {e}")

    # Try each installed browser; first one with cookies wins.
    for fn, label in (
        (browser_cookie3.brave, "brave"),
        (browser_cookie3.firefox, "firefox"),
        (browser_cookie3.chrome, "chrome"),
        (browser_cookie3.edge, "edge"),
    ):
        try:
            _grab(fn, label)
        except Exception as e:
            print(f"warn: {label} init: {e}")
        if cookies:
            print(f"using {label} cookies ({len(cookies)} found)")
            break

    if not cookies:
        raise SystemExit(
            "No cookies found in any browser (Brave/Firefox/Chrome/Edge). "
            "Are you logged into grok.com? Close the browser first so the cookie DB isn't locked."
        )

    # Preserve a sensible order: session JWTs first, then the rest.
    priority = ["sso", "sso-rw", "auth_token", "ct0", "x-userid", "cf_clearance", "grok_device_id"]
    ordered = sorted(cookies.items(), key=lambda kv: (priority.index(kv[0]) if kv[0] in priority else 99, kv[0]))
    return "; ".join(f"{k}={v}" for k, v in ordered)


def main() -> int:
    cookie_str = _collect()
    with open(SESSION_PATH, "w", encoding="utf-8") as fh:
        json.dump({"cookies": cookie_str}, fh, indent=2)
    print(f"Wrote {len(cookie_str)} bytes of cookies to {SESSION_PATH}")

    # Run the probe if available alongside this script.
    here = os.path.dirname(os.path.abspath(__file__))
    probe = os.path.join(here, "grok_probe.py")
    if os.path.exists(probe):
        import subprocess

        print("\n--- running grok_probe.py ---")
        return subprocess.call([sys.executable, probe])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
