"""Provider-quota fetcher registry (separate module to avoid circular imports)."""

from __future__ import annotations

from typing import Callable, Optional

# Provider id -> fetcher callable.  Order here is display priority in the footer.
PROVIDER_FETCHERS: dict[str, Callable[[], object]] = {}


def register(provider_id: str):
    def _wrap(fn: Callable[[], object]):
        PROVIDER_FETCHERS[provider_id] = fn
        return fn

    return _wrap


def get_fetcher(provider_id: str) -> Optional[Callable[[], object]]:
    return PROVIDER_FETCHERS.get(provider_id)
