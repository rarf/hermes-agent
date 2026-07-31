"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state (model, context %, cwd) and
appends it to the FINAL message of an agent turn when enabled.  Off by default
to keep replies minimal.

Config (``~/.hermes/config.yaml``)::

    display:
      runtime_footer:
        enabled: true                       # off by default
        fields: [model, context_pct, cwd]   # order shown; drop any to hide

Per-platform overrides live under ``display.platforms.<platform>.runtime_footer``.
Users can toggle the global setting with ``/footer on|off`` from both the CLI
and any gateway platform.

The footer is appended to the final response text in ``gateway/run.py`` right
before returning the response to the adapter send path — so it only lands on
the final message a user sees, not on tool-progress updates or streaming
partials.  When streaming is on and the final text has already been delivered
piecemeal, the footer is sent as a separate trailing message via
``send_trailing_footer()``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "
# Cache file freshness guard: if the quota cache is older than this, the
# footer drops the provider_quota field rather than showing stale numbers.
_QUOTA_CACHE_MAX_AGE_S = 60 * 30  # 30 minutes


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix for readability (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def resolve_footer_config(
    user_config: dict[str, Any] | None,
    platform_key: str | None = None,
) -> dict[str, Any]:
    """Resolve effective runtime-footer config for *platform_key*.

    Merge order (later wins):
        1. Built-in defaults (enabled=False)
        2. ``display.runtime_footer``
        3. ``display.platforms.<platform_key>.runtime_footer``
    """
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}

    global_cfg = cfg.get("runtime_footer")
    if isinstance(global_cfg, dict):
        if "enabled" in global_cfg:
            resolved["enabled"] = bool(global_cfg.get("enabled"))
        if isinstance(global_cfg.get("fields"), list) and global_cfg["fields"]:
            resolved["fields"] = [str(f) for f in global_cfg["fields"]]

    if platform_key:
        platforms = cfg.get("platforms") or {}
        plat_cfg = platforms.get(platform_key)
        if isinstance(plat_cfg, dict):
            plat_footer = plat_cfg.get("runtime_footer")
            if isinstance(plat_footer, dict):
                if "enabled" in plat_footer:
                    resolved["enabled"] = bool(plat_footer.get("enabled"))
                if isinstance(plat_footer.get("fields"), list) and plat_footer["fields"]:
                    resolved["fields"] = [str(f) for f in plat_footer["fields"]]

    return resolved


def format_runtime_footer(
    *,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
    quota_cache: Optional[dict[str, Any]] = None,
) -> str:
    """Render the footer, or return "" if no fields have data.

    Single-line fields (``model``, ``context_pct``, ``cwd``) are joined with
    `` · ``.  The ``provider_quota`` field renders as a multi-line block
    (one provider per line, each window with remaining % + reset) appended
    below the single-line summary.  Fields are skipped silently when their
    underlying data is missing — a partially-populated footer is better than a
    line with ``?%`` or empty slots.
    """
    parts: list[str] = []
    blocks: list[str] = []
    for field in fields:
        if field == "model":
            m = _model_short(model)
            if m:
                parts.append(m)
        elif field == "context_pct":
            if context_length and context_length > 0 and context_tokens >= 0:
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(f"{pct}%")
        elif field == "cwd":
            rel = _home_relative_cwd(cwd or os.environ.get("TERMINAL_CWD", ""))
            if rel:
                parts.append(rel)
        elif field == "provider_quota":
            qblock = _format_provider_quota(quota_cache)
            if qblock:
                blocks.append(qblock)
        # Unknown field names are silently ignored.

    lines: list[str] = []
    if parts:
        lines.append(_SEP.join(parts))
    lines.extend(blocks)
    if not lines:
        return ""
    return "\n".join(lines)


def _short_reset(reset_iso: Optional[str]) -> str:
    """Render an ISO reset timestamp as a short local 'reset <when>' string."""
    if not reset_iso:
        return ""
    try:
        dt = datetime.fromisoformat(reset_iso)
    except (ValueError, TypeError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    now = datetime.now()
    delta = (local.date() - now.date()).days
    if delta == 0:
        day = "today"
    elif delta == 1:
        day = "tomorrow"
    else:
        day = local.strftime("%b %d")
    return f"{day} {local.strftime('%H:%M')}"


def _format_provider_quota(quota_cache: Optional[dict[str, Any]]) -> str:
    """Render the per-provider quota block, or '' when no data.

    One provider per line.  Each provider shows every window (session / weekly /
    monthly) it reports, with remaining % and reset time.  Providers with no
    usable data show an ``unavailable`` note so the footer stays honest (no
    fake zeros).  Reads the precomputed quota cache (populated out-of-band by
    ``agent.quota_cache.refresh_quota_cache`` on a schedule) — the footer never
    does network I/O itself.
    """
    if not quota_cache:
        return ""
    providers = quota_cache.get("providers") or {}
    if not providers:
        return ""
    segs: list[str] = ["📊 quota:"]
    for name, rec in providers.items():
        if not isinstance(rec, dict):
            continue
        label = rec.get("label") or name
        reason = rec.get("unavailable_reason")
        windows = rec.get("windows") or []
        if not windows:
            # A provider with no windows and only a generic "no data" note adds
            # noise to an every-message footer — skip it silently.  An explicit
            # *unavailable* reason (e.g. auth failure, xAI oauth gap) is worth
            # surfacing so the user knows why it's missing.
            if reason in (None, "no data"):
                continue
            segs.append(f"• {label}: unavailable ({reason})")
            continue
        win_strs: list[str] = []
        for w in windows:
            wlabel = w.get("label") or "window"
            used = w.get("used_percent")
            if used is None:
                tail = _short_reset(w.get("reset_at"))
                win_strs.append(f"{wlabel}" + (f" (reset {tail})" if tail else ""))
                continue
            try:
                rem = str(max(0, min(100, round(100 - float(used)))))
            except (TypeError, ValueError):
                rem = "?"
            tail = _short_reset(w.get("reset_at"))
            win_strs.append(
                f"{wlabel} {rem}%" + (f" (reset {tail})" if tail else "")
            )
        segs.append(f"• {label}: " + " · ".join(win_strs))
    return "\n".join(segs)


def build_footer_line(
    *,
    user_config: dict[str, Any] | None,
    platform_key: str | None,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
) -> str:
    """Top-level entry point used by gateway/run.py.

    Returns the footer text (empty string when disabled or no data).  Callers
    append this to the final response themselves, preserving a single blank
    line of separation.
    """
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""

    # The provider_quota field reads a precomputed on-disk cache (never does
    # network I/O here).  Load it only when the field is actually requested so
    # we don't pay a file read for the common model/context/cwd-only footer.
    fields = cfg.get("fields") or _DEFAULT_FIELDS
    quota_cache: dict[str, Any] | None = None
    if "provider_quota" in fields:
        try:
            from agent.quota_cache import read_quota_cache, quota_cache_age_seconds

            if (quota_cache_age_seconds() or 10**9) <= _QUOTA_CACHE_MAX_AGE_S:
                quota_cache = read_quota_cache()
        except Exception:
            logger.debug("runtime_footer ▸ quota cache read failed", exc_info=True)

    return format_runtime_footer(
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        fields=fields,
        quota_cache=quota_cache,
    )
