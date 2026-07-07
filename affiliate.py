"""
affiliate.py
~~~~~~~~~~~~
EarnKaro (EK Affiliaters) affiliate-link conversion.

Converts a plain retailer product URL into an EarnKaro "profit" link via the
public EK Affiliaters converter API, so "back in stock" alerts can carry the
affiliate link instead of the raw URL.

Endpoint:   POST https://ekaro-api.affiliaters.in/api/converter/public
Auth:       Authorization: Bearer <EARNKARO_API_KEY>   (env var, never hardcoded)
Body:       {"deal": "<url>", "convert_option": "convert_only"}
Success:    {"success": 1, "data": "<converted deal text>", "randomPostID": "..."}

Design principles:
- Best-effort & non-fatal: convert_url() NEVER raises and returns None on any
  failure (missing key, network error, non-200, non-JSON, success != 1, or no
  URL in the converted text). Callers fall back to the original URL, so a
  conversion problem can never block or break a stock alert.
- The API key is read at call time from the environment (EARNKARO_API_KEY), so
  Railway's runtime value is always used and the key is never in the codebase.
- Amazon is intentionally out of scope here (handled separately via its own
  Associates tag); the eligibility gate lives in get_affiliate_url().
"""

import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_API_URL = "https://ekaro-api.affiliaters.in/api/converter/public"
_TIMEOUT_SECONDS = 15.0

# First http(s) URL in a string. EarnKaro's `data` field echoes the converted
# "deal" text; when we send a bare URL, the converted link is that text — we
# extract the first URL from it defensively in case any wrapper text is added.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


def _api_key() -> str:
    return os.environ.get("EARNKARO_API_KEY", "")


def _parse_response(data: object) -> str | None:
    """
    Pure parser for the EK Affiliaters response body (separated from the HTTP
    call so it's unit-testable without a network). Returns the converted URL
    from a successful response, or None for any non-success / malformed shape.
    """
    if not isinstance(data, dict):
        return None
    if data.get("success") != 1:
        return None
    converted_text = data.get("data")
    if not isinstance(converted_text, str) or not converted_text:
        return None
    m = _URL_RE.search(converted_text)
    return m.group(0) if m else None


async def convert_url(url: str) -> str | None:
    """
    Convert a single retailer URL to an EarnKaro affiliate link.
    Returns the converted URL, or None on ANY failure (caller falls back to the
    original URL). Never raises.
    """
    key = _api_key()
    if not key:
        logger.warning("[affiliate] EARNKARO_API_KEY not set — skipping conversion")
        return None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _API_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"deal": url, "convert_option": "convert_only"},
            )
    except Exception as exc:
        logger.error(f"[affiliate] request failed for {url!r}: {exc}")
        return None

    if resp.status_code != 200:
        logger.error(f"[affiliate] HTTP {resp.status_code} for {url!r}: {resp.text[:200]}")
        return None

    try:
        body = resp.json()
    except Exception:
        logger.error(f"[affiliate] non-JSON response for {url!r}: {resp.text[:200]!r}")
        return None

    converted = _parse_response(body)
    if converted is None:
        logger.error(f"[affiliate] conversion unsuccessful for {url!r}: {str(body)[:200]}")
        return None

    logger.info(f"[affiliate] converted {url!r} -> {converted!r}")
    return converted
