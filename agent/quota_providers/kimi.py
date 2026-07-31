"""Kimi (Kimi For Coding) quota fetcher.

Two auth paths (see CodexBar's kimi.md):
  1. API key: GET https://api.kimi.com/coding/v1/usages with Bearer token.
     Returns a weekly quota block (limit/used/remaining/resetTime) plus a
     5-hour rate-limit block (limits[].limit/used/remaining/resetTime).
  2. Web cookie fallback: POST .../BillingService/GetUsages with the `kimi-auth`
     cookie as Bearer. Same shape.

We read credentials from a LOCAL file the user controls (``~/kimi_session.json``)
so no secret ever touches the codebase.  The file may contain either
``{"api_key": "..."}`` (preferred) or ``{"token": "..."}`` (web cookie JWT).
Fail-open: any error -> QuotaResult(unavailable_reason=...).
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Optional

from .base import QuotaResult, QuotaWindow, build_unavailable

_API_URL = "https://api.kimi.com/coding/v1/usages"
_WEB_URL = "https://www.kimi.com/apiv2/kimi.gateway.billing.v1.BillingService/GetUsages"
_SESSION_PATH = os.path.join(os.path.expanduser("~"), "kimi_session.json")


def _load_creds() -> tuple[Optional[str], Optional[str]]:
    """Return (api_key, token) from ~/kimi_session.json, either may be None."""
    try:
        with open(_SESSION_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, None
    except Exception:
        return None, None
    if isinstance(data, dict):
        return data.get("api_key"), data.get("token")
    return None, None


def _parse_block(block: dict) -> QuotaWindow:
    label = block.get("scope") or block.get("window") or "window"
    detail = block.get("detail") or {}
    limit = block.get("limit", detail.get("limit"))
    used = block.get("used", detail.get("used"))
    remaining = block.get("remaining", detail.get("remaining"))
    reset = block.get("resetTime", detail.get("resetTime"))
    # Derive used_percent from used/limit when the API omits it.
    used_pct: Optional[float] = None
    if used is not None and limit not in (None, 0):
        try:
            used_pct = round(100.0 * float(used) / float(limit), 2)
        except (TypeError, ValueError):
            used_pct = None
    # Prefer explicit remaining if present.
    if remaining is not None and limit not in (None, 0):
        try:
            used_pct = round(100.0 * (1 - float(remaining) / float(limit)), 2)
        except (TypeError, ValueError):
            pass
    return QuotaWindow(label=str(label), used_percent=used_pct, reset_at=reset)


def _fetch_with(headers: dict, url: str, method: str = "GET", body: Optional[bytes] = None) -> Optional[QuotaResult]:
    req = urllib.request.Request(url, headers=headers, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return build_unavailable("kimi", "auth-failed")
        return build_unavailable("kimi", f"http-{e.code}")
    except Exception as e:
        return build_unavailable("kimi", f"fetch-error:{type(e).__name__}")
    try:
        data = json.loads(raw)
    except Exception:
        return build_unavailable("kimi", "bad-json")

    windows: list[QuotaWindow] = []
    # Weekly (or tier) usage block.
    if isinstance(data.get("usage"), dict):
        windows.append(_parse_block(data["usage"]))
    # 5-hour rate-limit block(s).
    limits = data.get("limits")
    if isinstance(limits, list):
        for blk in limits:
            if isinstance(blk, dict):
                windows.append(_parse_block(blk))
    elif isinstance(limits, dict):
        windows.append(_parse_block(limits))

    if not windows:
        return build_unavailable("kimi", "no-data")
    return QuotaResult(label="kimi", windows=windows, plan=None, unavailable_reason=None)


def fetch_kimi_quota() -> QuotaResult:
    api_key, token = _load_creds()
    if api_key:
        return _fetch_with({"Authorization": f"Bearer {api_key}"}, _API_URL, "GET") or build_unavailable("kimi", "no-data")
    if token:
        # Web fallback: POST with the JWT as Bearer.
        return _fetch_with({"Authorization": f"Bearer {token}"}, _WEB_URL, "POST", b"{}") or build_unavailable("kimi", "no-data")
    return build_unavailable("kimi", "no-credentials")


from .registry import register as _register  # noqa: E402

_register("kimi")(fetch_kimi_quota)
