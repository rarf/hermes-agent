"""Quota / rate-limit cache for the runtime footer.

The gateway runtime footer (``gateway/runtime_footer.py``) can show a per-provider
quota block (``provider_quota`` field) — one provider per line, each window
(weekly / monthly / session) with its remaining % and reset time.  Showing live
quota on every final message would mean N network calls per reply (one per
provider), plus the footer has no live agent / credentials in scope.  Instead:

  * this module is the *only* place that talks to provider quota APIs;
  * ``refresh_quota_cache()`` is run on a schedule (cron) and writes a small
    JSON summary to ``$HERMES_HOME/quota_cache.json``;
  * the footer reads that JSON (``read_quota_cache()``) — pure, offline, fast.

Supported providers piggy-back on the existing ``agent.account_usage`` fetchers
(``nous``, ``openai-codex``, ``anthropic``, ``openrouter``) plus a best-effort
xAI fetch.  Any provider whose fetch fails or is unsupported is recorded with an
``unavailable_reason`` so the footer shows it honestly (no fake zeros).

Cache schema (``quota_cache.json``)::

    {
      "fetched_at": "2026-07-31T12:00:00+00:00",
      "providers": {
        "openai-codex": {
          "label": "Account limits",
          "plan": "Plus",
          "unavailable_reason": null,
          "windows": [
            {"label": "Session", "used_percent": 100.0,
             "reset_at": "2026-08-05T07:00:41+00:00"}
          ]
        },
        "xai-oauth": {
          "label": "xai-oauth",
          "plan": null,
          "unavailable_reason": "oauth-no-billing-access",
          "windows": []
        }
      }
    }

``used_percent`` is the provider-reported *used* fraction (0..100); the footer
renders ``remaining = 100 - used``.  ``reset_at`` is an ISO-8601 UTC timestamp.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "quota_cache.json"
_CACHE_LOCK = threading.Lock()

# Providers we know how to fetch quota for, in display priority order.
_SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "nous",
    "openai-codex",
    "anthropic",
    "openrouter",
    "xai-oauth",
)


def _cache_path() -> str:
    return os.path.join(str(get_hermes_home()), _CACHE_FILENAME)


def read_quota_cache() -> dict[str, Any]:
    """Return the parsed quota cache, or an empty shell if missing/unreadable.

    Never raises — a corrupt or absent cache degrades to "no data" so the
    footer simply omits the quota field instead of crashing the reply.
    """
    try:
        with open(_cache_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("quota_cache ▸ read failed (degrade to empty)", exc_info=True)
    return {"fetched_at": None, "providers": {}}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_to_record(win: Any) -> dict[str, Any]:
    """Flatten one ``AccountUsageWindow`` into a JSON-friendly dict."""
    reset = getattr(win, "reset_at", None)
    reset_iso: Optional[str] = None
    if isinstance(reset, datetime):
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        reset_iso = reset.isoformat()
    used = getattr(win, "used_percent", None)
    if used is not None:
        try:
            used = float(used)
        except (TypeError, ValueError):
            used = None
    return {
        "label": str(getattr(win, "label", "") or ""),
        "used_percent": used,
        "reset_at": reset_iso,
    }


def _summarize_snapshot(snapshot: Any) -> dict[str, Any]:
    """Flatten an ``AccountUsageSnapshot`` into the cache's record.

    Preserves *every* window (session / weekly / monthly) with its own
    used-percent and reset time, so the footer can render them on separate
    lines.  A window with no ``used_percent`` is kept but marked unavailable
    (so the footer can still show its reset time if present).
    """
    provider = getattr(snapshot, "provider", "unknown")
    windows = [w for w in (getattr(snapshot, "windows", ()) or ())]
    records = [_window_to_record(w) for w in windows]

    return {
        # Use the provider key (not the generic snapshot title like "Account
        # limits") so the footer labels each line by provider name.
        "label": str(provider),
        "plan": getattr(snapshot, "plan", None),
        "unavailable_reason": getattr(snapshot, "unavailable_reason", None),
        "windows": records,
    }


def _fetch_xai_quota() -> Optional[dict[str, Any]]:
    """Best-effort xAI quota fetch.

    The xAI *OAuth* token (SuperGrok) does not grant access to the API billing
    endpoints — those require a separate API key (see x.ai docs).  We read the
    OAuth token from the credential pool and try the ``/v1/usage`` endpoint
    anyway; if there's no token or it 401s, we report ``oauth-no-billing-access``
    rather than faking a number.

    Returns a provider record dict, or None if we couldn't even attempt.
    """
    try:
        auth_path = os.path.join(str(get_hermes_home()), "auth.json")
        with open(auth_path, "r", encoding="utf-8") as fh:
            auth = json.load(fh)
        pool = (auth.get("credential_pool") or {}).get("xai-oauth") or []
        token: Optional[str] = None
        if pool:
            toks = pool[0].get("tokens") or {}
            token = toks.get("access_token") or toks.get("token")
        if not token:
            return {
                "label": "xai-oauth",
                "plan": None,
                "unavailable_reason": "oauth-no-token",
                "windows": [],
            }
        import urllib.request

        url = "https://api.x.ai/v1/usage"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read().decode("utf-8", "replace")
            data = json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {
                    "label": "xai-oauth",
                    "plan": None,
                    "unavailable_reason": "oauth-no-billing-access",
                    "windows": [],
                }
            return {
                "label": "xai-oauth",
                "plan": None,
                "unavailable_reason": f"http-{e.code}",
                "windows": [],
            }
        except Exception as e:  # pragma: no cover - network
            return {
                "label": "xai-oauth",
                "plan": None,
                "unavailable_reason": f"fetch-error:{type(e).__name__}",
                "windows": [],
            }
        # The /v1/usage payload shape varies; surface whatever credit info we
        # can find, otherwise just record that we reached the endpoint.
        return {
            "label": "xai-oauth",
            "plan": None,
            "unavailable_reason": "usage-shape-unknown",
            "windows": [],
            "_raw": bool(data),
        }
    except Exception:
        logger.debug("quota_cache ▸ xai fetch crashed", exc_info=True)
        return {
            "label": "xai-oauth",
            "plan": None,
            "unavailable_reason": "fetch-error",
            "windows": [],
        }


def refresh_quota_cache(*, timeout: float = 12.0) -> dict[str, Any]:
    """Fetch quota for every supported provider and write the cache file.

    Fail-open per provider: a fetch error leaves that provider with
    ``windows=[]`` + an ``unavailable_reason`` rather than aborting the whole
    refresh.  Returns the cache dict that was written (also handy for tests /
    dry runs).
    """
    providers: dict[str, Any] = {}

    try:
        from agent.account_usage import fetch_account_usage
    except Exception:
        logger.debug("quota_cache ▸ cannot import fetchers", exc_info=True)
        fetch_account_usage = None  # type: ignore[assignment]

    for provider in _SUPPORTED_PROVIDERS:
        if provider == "xai-oauth":
            rec = _fetch_xai_quota()
            if rec is None:
                rec = {
                    "label": "xai-oauth",
                    "plan": None,
                    "unavailable_reason": "no data",
                    "windows": [],
                }
            providers[provider] = rec
            continue

        rec: dict[str, Any] = {
            "label": provider,
            "plan": None,
            "unavailable_reason": None,
            "windows": [],
        }
        if fetch_account_usage is None:
            rec["unavailable_reason"] = "fetcher unavailable"
        else:
            try:
                snap = fetch_account_usage(provider)
                if snap is None:
                    rec["unavailable_reason"] = "no data"
                else:
                    rec = _summarize_snapshot(snap)
            except Exception:
                logger.debug("quota_cache ▸ fetch failed for %s", provider, exc_info=True)
                rec["unavailable_reason"] = "fetch error"
        providers[provider] = rec

    cache = {"fetched_at": _utc_now_iso(), "providers": providers}

    try:
        with _CACHE_LOCK:
            path = _cache_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
    except Exception:
        logger.debug("quota_cache ▸ write failed", exc_info=True)

    return cache


def quota_cache_age_seconds() -> Optional[float]:
    """Seconds since the cache was fetched, or None if absent/invalid."""
    data = read_quota_cache()
    ts = data.get("fetched_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    result = refresh_quota_cache()
    print(json.dumps(result, indent=2, sort_keys=True))
