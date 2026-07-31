"""Local Grok quota probe — run this YOURSELF, not the agent.

Usage:
  1. Create ~/grok_session.json containing your grok.com browser session cookies:
       { "cookies": "i18nextLng=en; sso=...; sso-rw=...; x-userid=...; cf_clearance=...; ..." }
     (copy the full Cookie header value from DevTools -> Network -> GetGrokCreditsConfig -> Request Headers)
  2. Run:  python grok_probe.py
  3. It prints the HTTP status + saves the raw response to ~/grok_last_response.bin
     and a hex/base64 dump to stdout.  Send the HEX (not your cookies!) to the agent
     so the protobuf parser can be tightened.  Your cookies stay in the local file.

Why local-only: the grok.com billing endpoint requires your live browser session
cookies (Cloudflare + sso JWT).  Those are secrets — they must never be pasted
into chat.  This script keeps them on your machine.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
import urllib.error

ENDPOINT = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
BODY = b"\x00\x00\x00\x00\x00"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
SESSION_PATH = os.path.join(os.path.expanduser("~"), "grok_session.json")
RAW_PATH = os.path.join(os.path.expanduser("~"), "grok_last_response.bin")


def main() -> int:
    if not os.path.exists(SESSION_PATH):
        print(f"ERROR: {SESSION_PATH} not found. Create it with your cookies (see docstring).")
        return 2
    data = json.load(open(SESSION_PATH, "r", encoding="utf-8"))
    cookies = data.get("cookies") or data
    if isinstance(cookies, dict):
        cookies = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if not str(cookies).strip():
        print("ERROR: no cookies in session file")
        return 2

    headers = {
        "accept": "*/*",
        "content-type": "application/grpc-web+proto",
        "origin": "https://grok.com",
        "referer": "https://grok.com/?_s=usage",
        "user-agent": UA,
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "cookie": str(cookies),
    }
    req = urllib.request.Request(ENDPOINT, data=BODY, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        print(f"HTTP {status}")
        print("BODY (hex):", raw[:400].hex())
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1

    with open(RAW_PATH, "wb") as fh:
        fh.write(raw)
    print(f"HTTP {status}, {len(raw)} bytes saved to {RAW_PATH}")
    print("BASE64 (paste this to the agent — NOT your cookies):")
    print(base64.b64encode(raw).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
