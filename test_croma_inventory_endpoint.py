"""
One-off diagnostic: call Croma's authenticated inventory/delivery endpoint
with real session headers, to discover the request payload shape and
response schema for pincode-specific stock checking.

Not part of the app — bot.py never imports this. Calls
POST https://api.croma.com/inventory/oms/v2/tms/details-pwa/.

The first run of this script (direct, no proxy) got an identical 403
"Access Denied" HTML page back for all 3 payload guesses — the signature of
an Akamai edge/WAF block on Railway's IP, not a real API validation error
(see the Server header check below). This version can instead route the
same request through Scrape.do with customHeaders=True, which forwards
whatever headers you attach through to the target untouched (confirmed via
Scrape.do's own "Custom Headers" docs) — the auth headers stay the same,
only the source IP changes.

CAVEAT: Scrape.do's docs confirm header forwarding for this use case, but
NOT explicitly whether the HTTP method and POST body of your request to
Scrape.do are also mirrored through to the target the same way (every such
proxy-URL API we're aware of works this way, but it's unconfirmed for
Scrape.do specifically). If the via-Scrape.do response looks like Croma
processed a POST with our JSON body (a real validation error mentioning our
fields, or actual data), that's confirmed. If it looks like Croma received
an empty/GET request instead (e.g. a generic "missing parameters" error
identical regardless of payload), method/body forwarding isn't working and
needs Scrape.do support's input, not another guess here.

We also don't have a captured real request body (the HAR export didn't
include POST bodies — see the earlier HAR review), so this tries a few
plausible payload shapes and prints each one's full response. If every
guess gets a 400/422 (and isn't an Akamai block), read that error body
carefully: many APIs list the specific missing/invalid fields in the
validation error, which is itself the information we need.

Usage (run via `railway run python3 test_croma_inventory_endpoint.py`,
wherever the real session env vars are set):

    CROMA_ACCESS_TOKEN=xxx CROMA_CUSTOMER_HASH=xxx CROMA_APIM_SUBSCRIPTION_KEY=xxx \\
        python3 test_croma_inventory_endpoint.py <product_id> <pincode>

    # Route through Scrape.do instead of calling Croma directly:
    CROMA_ACCESS_TOKEN=xxx CROMA_CUSTOMER_HASH=xxx CROMA_APIM_SUBSCRIPTION_KEY=xxx \\
    CROMA_USE_SCRAPEDO=1 SCRAPEDO_KEY=xxx \\
        python3 test_croma_inventory_endpoint.py 322520 140301

    # or, once you've seen the guesses fail and want to try your own shape:
    CROMA_PAYLOAD_JSON='{"pinCode": "140301", "productSkus": ["322520"]}' \\
        python3 test_croma_inventory_endpoint.py 322520 140301

Env vars:
    CROMA_ACCESS_TOKEN            required — the "accessToken" header value
    CROMA_CUSTOMER_HASH           required — the "customerHash" header value
    CROMA_APIM_SUBSCRIPTION_KEY   required — the "oms-apim-subscription-key" header value
    CROMA_CLIENT_ID               optional — defaults to "CROMA-WEB-APP"
    CROMA_PAYLOAD_JSON            optional — if set, skip the 3 built-in
                                   guesses and send exactly this JSON body instead
    CROMA_USE_SCRAPEDO             optional — if set to any non-empty value,
                                   route the request through Scrape.do
                                   (requires SCRAPEDO_KEY) instead of calling
                                   Croma directly
    SCRAPEDO_KEY                  required only when CROMA_USE_SCRAPEDO is set
"""

import asyncio
import json
import os
import sys

import httpx

# Script sits at the repo root, so this import resolves the same way
# compare_flipkart_render.py's does.
from checkers.common import build_scraper_url

ENDPOINT = "https://api.croma.com/inventory/oms/v2/tms/details-pwa/"

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.croma.com",
    "Referer": "https://www.croma.com/",
}


def _build_headers() -> dict:
    access_token = os.environ.get("CROMA_ACCESS_TOKEN", "")
    customer_hash = os.environ.get("CROMA_CUSTOMER_HASH", "")
    apim_key = os.environ.get("CROMA_APIM_SUBSCRIPTION_KEY", "")
    client_id = os.environ.get("CROMA_CLIENT_ID", "CROMA-WEB-APP")

    missing = [
        name for name, val in (
            ("CROMA_ACCESS_TOKEN", access_token),
            ("CROMA_CUSTOMER_HASH", customer_hash),
            ("CROMA_APIM_SUBSCRIPTION_KEY", apim_key),
        ) if not val
    ]
    if missing:
        print(f"Missing required env var(s): {', '.join(missing)}")
        sys.exit(1)

    return {
        **_BASE_HEADERS,
        "accessToken": access_token,
        "customerHash": customer_hash,
        "client_id": client_id,
        "oms-apim-subscription-key": apim_key,
    }


def _candidate_payloads(product_id: str, pincode: str) -> list[tuple[str, dict]]:
    """
    Plausible request bodies, labeled by rationale. We know the real captured
    request was ~818 bytes (much richer than any of these), so don't expect
    a clean 200 on the first try — the goal is to provoke a response (success
    or a descriptive validation error) that reveals the real shape.
    """
    return [
        (
            "A: mirrors essentialcombo's own param names (pinCode / ProductSkus)",
            {"pinCode": pincode, "productSkus": [product_id]},
        ),
        (
            "B: minimal singular field names",
            {"pincode": pincode, "productId": product_id},
        ),
        (
            "C: cart/line-item style (oms/tms suggests order-mgmt + time-slot context)",
            {"pincode": pincode, "items": [{"sku": product_id, "quantity": 1}], "channel": "WEB"},
        ),
    ]


def _print_response(label: str, status: int, headers: httpx.Headers, body_text: str) -> None:
    print(f"\n=== {label} ===")
    print(f"  HTTP status: {status}")
    print(f"  Content-Type: {headers.get('content-type')}")
    print(f"  Response byte size: {len(body_text.encode('utf-8'))}")
    server = headers.get("server", "")
    print(f"  Server header: {server!r}" + ("  <-- Akamai edge block signature" if "akamai" in server.lower() else ""))
    print(f"  All response headers: {dict(headers)}")
    try:
        parsed = json.loads(body_text)
        print("  Response JSON:")
        print(json.dumps(parsed, indent=2)[:4000])
    except json.JSONDecodeError:
        print("  Response body (not valid JSON, raw text):")
        print(body_text[:2000])


async def _try_payload(
    client: httpx.AsyncClient, request_url: str, headers: dict, label: str, payload: dict
) -> None:
    print(f"\n--- Trying payload {label} ---")
    print(f"  Request body: {json.dumps(payload)}")
    try:
        resp = await client.post(request_url, headers=headers, json=payload, timeout=60.0)
    except Exception as exc:
        print(f"  Request failed: {exc}")
        return
    _print_response(label, resp.status_code, resp.headers, resp.text)


def _resolve_request_url(via_scrapedo: bool) -> str:
    if not via_scrapedo:
        return ENDPOINT
    if not os.environ.get("SCRAPEDO_KEY"):
        print("CROMA_USE_SCRAPEDO is set but SCRAPEDO_KEY is missing.")
        sys.exit(1)
    return build_scraper_url(ENDPOINT, custom_headers=True)


async def main():
    if len(sys.argv) < 3:
        print("Usage: python3 test_croma_inventory_endpoint.py <product_id> <pincode>")
        sys.exit(1)
    product_id, pincode = sys.argv[1], sys.argv[2]
    headers = _build_headers()
    via_scrapedo = bool(os.environ.get("CROMA_USE_SCRAPEDO"))
    request_url = _resolve_request_url(via_scrapedo)

    print(f"Target endpoint: {ENDPOINT}")
    print(f"Routing: {'via Scrape.do (customHeaders=true)' if via_scrapedo else 'direct (no proxy)'}")
    if via_scrapedo:
        print(f"  Scrape.do URL (truncated): {request_url[:100]!r}")
    print(f"product_id={product_id!r} pincode={pincode!r}")
    print(f"client_id header: {headers['client_id']!r}")

    custom_payload_raw = os.environ.get("CROMA_PAYLOAD_JSON")
    async with httpx.AsyncClient() as client:
        if custom_payload_raw:
            try:
                custom_payload = json.loads(custom_payload_raw)
            except json.JSONDecodeError as exc:
                print(f"CROMA_PAYLOAD_JSON is not valid JSON: {exc}")
                sys.exit(1)
            await _try_payload(client, request_url, headers, "custom (from CROMA_PAYLOAD_JSON)", custom_payload)
        else:
            for label, payload in _candidate_payloads(product_id, pincode):
                await _try_payload(client, request_url, headers, label, payload)

    if via_scrapedo:
        print(
            "\nCheck whether the response looks like Croma actually processed "
            "a POST with our JSON body (a real validation error naming our "
            "fields, or actual inventory data) versus a generic error "
            "identical across payloads (would mean the method/body weren't "
            "mirrored through Scrape.do the way we assumed) versus still an "
            "Akamai-style block (would mean Scrape.do's IP for this request "
            "is also flagged)."
        )
    else:
        print(
            "\nIf all attempts returned an error, paste the full output back — "
            "check the Server header first (Akamai block vs real API error), "
            "then validation error bodies are the fastest way to learn the "
            "real required field names without another HAR capture."
        )


if __name__ == "__main__":
    asyncio.run(main())
