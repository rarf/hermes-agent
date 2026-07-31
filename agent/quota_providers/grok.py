"""Grok (X Premium / SuperGrok) quota fetcher.

Grok billing is exposed through two surfaces (see CodexBar's grok.md):
  1. ``grok agent stdio`` ACP JSON-RPC ``x.ai/billing`` — structured, but disabled
     in current grok CLI builds (returns "Method not found").
  2. grok.com gRPC-web fallback: POST an empty message to
     ``https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig`` with
     the browser session cookies.  The response is gRPC-web framed protobuf
     carrying ``credit_usage_percent`` + a reset timestamp.

This fetcher implements path (2).  It reads the browser session cookies from a
LOCAL file the user controls (``~/grok_session.json`` — NOT committed, NOT sent
to the agent) so no live secret ever touches the codebase.  When the response
can't be parsed yet, it dumps the raw bytes to ``~/grok_last_response.bin`` so the
caller can inspect and we can tighten the protobuf parser without round-trips.

Fail-open: any error -> QuotaResult(unavailable_reason=...).
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

_GROK_ENDPOINT = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
_EMPTY_GRPCWEB_BODY = b"\x00\x00\x00\x00\x00"  # 0x00 frame + 4-byte len(0)
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

# Local session file the user populates (cookies only).  Kept out of the repo.
_SESSION_PATH = os.path.join(os.path.expanduser("~"), "grok_session.json")
_RAW_DEBUG_PATH = os.path.join(os.path.expanduser("~"), "grok_last_response.bin")


def _load_cookies() -> Optional[str]:
    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        cookies = data.get("cookies") or data  # accept bare cookie string or {"cookies": "..."}
        if isinstance(cookies, dict):
            # dict form: one entry per cookie name
            cookies = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if not cookies or not str(cookies).strip():
            return None
        return str(cookies)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _parse_grok_protobuf(raw: bytes) -> Optional[QuotaResult]:
    """Parse the gRPC-web GetGrokCreditsConfig protobuf response.

    Verified wire layout (from a real response sample):
      top message:
        field 1 (len) -> inner message:
          field 1 (fixed32 float) = credit_usage_percent (0..100)
          field 4 (len) -> { field 1 (varint) = window_start epoch seconds,
                              field 2 (varint) = duration }
          field 5 (len) -> { field 1 (varint) = window_end epoch seconds,
                              field 2 (varint) = duration }
          field 7 (len) -> { field 1 (varint)=2, field 2 (fixed32 float)=100.0 }
    The weekly reset is the later of field 4.1 / field 5.1 (both epoch seconds,
    7 days apart).  Remaining % = 100 - credit_usage_percent.
    """
    import struct

    # Strip gRPC-web frame header if present.
    msg = raw
    if msg[:1] == b"\x00" and len(msg) >= 5:
        length = int.from_bytes(msg[1:5], "big")
        if length and len(msg) >= 5 + length:
            msg = msg[5 : 5 + length]

    def parse(m):
        out = []
        i = 0
        while i < len(m):
            if i >= len(m):
                break
            key = m[i]
            i += 1
            fn = key >> 3
            wire = key & 0x07
            if wire == 0:
                v = 0
                s = 0
                while i < len(m):
                    b = m[i]
                    i += 1
                    v |= (b & 0x7F) << s
                    s += 7
                    if not (b & 0x80):
                        break
                out.append((fn, wire, v))
            elif wire == 2:
                ln = 0
                s = 0
                while i < len(m):
                    b = m[i]
                    i += 1
                    ln |= (b & 0x7F) << s
                    s += 7
                    if not (b & 0x80):
                        break
                d = m[i : i + ln]
                i += ln
                out.append((fn, wire, d))
            elif wire == 5:
                v = struct.unpack("<f", m[i : i + 4])[0]
                i += 4
                out.append((fn, wire, v))
            elif wire == 1:
                v = int.from_bytes(m[i : i + 8], "little")
                i += 8
                out.append((fn, wire, v))
            else:
                break
        return out

    top = parse(msg)
    if not top or top[0][0] != 1 or top[0][1] != 2:
        return None
    inner = parse(top[0][2])

    # Verified against a real response where the account had hit 100% of its
    # weekly limit: field 1 (fixed32 float) IS credit_usage_percent (used%).
    # The weekly window (fields 4/5) carries the reset timestamp (epoch seconds,
    # ~7 days apart).  remaining% = 100 - used.
    used_percent: Optional[float] = None
    reset_epoch: Optional[int] = None

    for fn, wire, v in inner:
        if fn == 1 and wire == 5:  # credit_usage_percent (float, 0..100, USED)
            try:
                used_percent = float(v)
            except (TypeError, ValueError):
                pass
        elif fn in (4, 5) and wire == 2:  # window start/end sub-messages
            sub = parse(v)
            for sfn, sw, sv in sub:
                if sfn == 1 and sw == 0:  # epoch seconds (window end = next reset)
                    if reset_epoch is None or sv > reset_epoch:
                        reset_epoch = sv

    if used_percent is None and reset_epoch is None:
        return None

    reset_iso = None
    if reset_epoch is not None:
        try:
            from datetime import datetime, timezone

            reset_iso = datetime.fromtimestamp(reset_epoch, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            reset_iso = None

    win = QuotaWindow(label="Weekly", used_percent=used_percent, reset_at=reset_iso)
    return QuotaResult(label="grok", windows=[win], plan=None, unavailable_reason=None)


def fetch_grok_quota() -> QuotaResult:
    cookies = _load_cookies()
    if not cookies:
        return build_unavailable("grok", "no-session-cookies")

    headers = {
        "accept": "*/*",
        "content-type": "application/grpc-web+proto",
        "origin": "https://grok.com",
        "referer": "https://grok.com/?_s=usage",
        "user-agent": _BROWSER_UA,
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "cookie": cookies,
    }
    req = urllib.request.Request(_GROK_ENDPOINT, data=_EMPTY_GRPCWEB_BODY, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # Cloudflare / auth block — session cookie stale or fingerprint rejected.
            return build_unavailable("grok", "cloudflare-blocked" if e.code == 403 else "auth-failed")
        return build_unavailable("grok", f"http-{e.code}")
    except Exception as e:
        return build_unavailable("grok", f"fetch-error:{type(e).__name__}")

    # Save raw for inspection so we can tighten the parser without re-fetching.
    try:
        with open(_RAW_DEBUG_PATH, "wb") as fh:
            fh.write(raw)
    except Exception:
        pass

    result = _parse_grok_protobuf(raw)
    if result is None:
        # Got a response but couldn't parse it yet — keep the raw, report pending.
        return build_unavailable("grok", "parse-pending")
    return result


# Register with the provider fetcher registry.
from .registry import register as _register  # noqa: E402

_register("grok")(fetch_grok_quota)
