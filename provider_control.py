"""
provider_control.py
~~~~~~~~~~~~~~~~~~~
Which fetch provider Amazon and Flipkart use, switchable at runtime.

* The active provider and the "if proxy fails, use Scrape.do" toggle live in
  the database (app_settings table) so a change survives restarts and is
  shared by the bot, the Telegram /provider command and the web dashboard.
* config.SCRAPING_PROVIDER (env) is only the DEFAULT, used when nothing is
  stored — or when the stored provider is no longer configured (e.g.
  PROXY_URL was removed), in which case the bot keeps working on the default
  instead of failing.
* Every other store ignores all of this and keeps using
  config.SCRAPING_PROVIDER exactly as before (see checkers/common.py).

Pure of Telegram/Flask: the bot and the dashboard both call the functions
below, so the rules (validation, cache clear, logging) exist exactly once.
"""

from __future__ import annotations

import logging
import os

import proxy_client
from config import SCRAPING_PROVIDER
from database import get_setting, set_setting

logger = logging.getLogger(__name__)

PROVIDERS = ("scrapedo", "zyte", "proxy")
LABELS = {"scrapedo": "Scrape.do", "zyte": "Zyte", "proxy": "Proxy"}

# Only these stores use the switchable provider (and the "inconclusive on
# failure/blocked page" behaviour). Everything else is unchanged.
SWITCHABLE_SITES = frozenset({"amazon", "flipkart"})

SETTING_PROVIDER = "scraping_provider"
SETTING_FALLBACK = "proxy_fallback_scrapedo"

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


def is_configured(name: str) -> bool:
    """Whether `name` has the credentials it needs. Never logs secrets."""
    if name == "scrapedo":
        return bool(os.environ.get("SCRAPEDO_KEY", "").strip())
    if name == "zyte":
        return bool(os.environ.get("ZYTE_API_KEY", "").strip())
    if name == "proxy":
        return proxy_client.proxy_configured()
    return False


def default_provider() -> str:
    """The env-configured default (config.SCRAPING_PROVIDER). Anything
    unrecognised keeps the old behaviour of the original code: Scrape.do.
    "proxy" is only honoured when it is actually configured."""
    value = (SCRAPING_PROVIDER or "").strip().lower()
    if value == "zyte":
        return "zyte"
    if value == "proxy" and proxy_client.proxy_configured():
        return "proxy"
    return "scrapedo"


def get_stored_provider() -> str | None:
    value = get_setting(SETTING_PROVIDER)
    return value if value in PROVIDERS else None


def get_active_provider() -> str:
    """Provider Amazon/Flipkart fetches use RIGHT NOW. Never raises."""
    stored = get_stored_provider()
    if stored:
        if is_configured(stored):
            return stored
        _warn_once(
            f"unconfigured:{stored}",
            f"[provider] stored provider {stored!r} is not configured any more "
            f"(missing credentials) — using the default {default_provider()!r} instead.",
        )
    return default_provider()


def get_fallback_enabled() -> bool:
    return get_setting(SETTING_FALLBACK, "0") == "1"


def fallback_effective() -> bool:
    """Fallback is on AND Scrape.do can actually be used."""
    if not get_fallback_enabled():
        return False
    if not is_configured("scrapedo"):
        _warn_once("fallback-no-key", "[provider] fallback is ON but SCRAPEDO_KEY is not set — it cannot be used.")
        return False
    return True


def get_state() -> dict:
    """Everything the Telegram command and the dashboard need to render."""
    return {
        "active": get_active_provider(),
        "stored": get_stored_provider(),
        "default": default_provider(),
        "fallback": get_fallback_enabled(),
        "fallback_usable": is_configured("scrapedo"),
        "configured": {name: is_configured(name) for name in PROVIDERS},
        "labels": dict(LABELS),
        "sites": sorted(SWITCHABLE_SITES),
    }


def _clear_fetch_cache() -> None:
    # Imported lazily: stock_checker imports this module, so a top-level
    # import here would be circular.
    try:
        import stock_checker

        stock_checker.clear_fetch_cache()
    except Exception as exc:  # never let a cache problem block a switch
        logger.error(f"[provider] could not request a fetch-cache clear: {exc}")


def set_active_provider(name: str, source: str) -> tuple[bool, str]:
    """Switch the provider. `source` says who did it ("telegram:<id>",
    "dashboard") and is logged. Returns (ok, message for the admin)."""
    name = (name or "").strip().lower()
    if name not in PROVIDERS:
        return False, f"Unknown provider {name!r}."
    label = LABELS[name]
    if not is_configured(name):
        needs = {"scrapedo": "SCRAPEDO_KEY", "zyte": "ZYTE_API_KEY", "proxy": "PROXY_URL"}[name]
        return False, f"{label} is not configured (set {needs}). Provider unchanged."

    old = get_active_provider()
    set_setting(SETTING_PROVIDER, name)
    if old == name:
        return True, f"{label} was already active."

    _clear_fetch_cache()
    logger.info(f"[provider] active provider changed: {old} -> {name} (by {source}); fetch cache clear requested")
    return True, f"Provider switched: {LABELS.get(old, old)} → {label}. Fetch cache cleared."


def set_fallback_enabled(enabled: bool, source: str) -> tuple[bool, str]:
    old = get_fallback_enabled()
    set_setting(SETTING_FALLBACK, "1" if enabled else "0")
    if old != enabled:
        logger.info(f"[provider] proxy->Scrape.do fallback {'ON' if enabled else 'OFF'} (by {source})")
    msg = f"Fallback (if proxy fails → Scrape.do) is now {'ON' if enabled else 'OFF'}."
    if enabled and not is_configured("scrapedo"):
        msg += " Warning: SCRAPEDO_KEY is not set, so it cannot be used yet."
    return True, msg
