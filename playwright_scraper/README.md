# playwright_scraper

Standalone pilot: a self-hosted Playwright + Chromium scraper for **iQOO and
Vivo only**, to test replacing their current Scrape.do `render=true` checks
(which burn render credits at scale) with a self-hosted equivalent behind an
optional metered residential proxy.

**Not wired into the main bot.** The main bot's existing `checkers/iqoo.py`
and `checkers/vivo.py` (Scrape.do-based) are completely untouched and remain
live in production. This service exists purely for you to test standalone —
hit it directly, compare its results against real stock status, and decide
whether/how to integrate it later. If this service crashes, gets blocked by
the target site, or a proxy runs dry, nothing about the main bot changes —
nothing in the main bot calls this service (yet).

## Deploying on Railway

Same pattern as `whatsapp_forwarder/`:

1. **New Service → GitHub Repo** → same repo, **Root Directory**
   `playwright_scraper/`.
2. **Builder: Dockerfile** (ships its own Dockerfile based on Playwright's
   official image — has Chromium and every system dependency already
   installed; Railway's default nixpacks builder does not).
3. No volume needed — this service is fully stateless.
4. Environment variables (all optional, sensible defaults):
   - `MAX_CONCURRENT_CHECKS` (default `2`) — how many browser instances may
     run at once. Each headless Chromium instance can use 150-300MB+ RAM;
     this bounds total memory use under concurrent load. Requests beyond
     the limit queue for a free slot (up to `SLOT_WAIT_TIMEOUT_SECONDS`,
     default 60s) rather than spawning unbounded browsers.
   - `MAX_RETRIES` (default `3`) — retry attempts before giving up and
     returning a "check failed" result (`in_stock: null`), never a guessed
     `false`.
   - `RETRY_DELAY_SECONDS` (default `2`) — pause between retry attempts.
   - `NAV_TIMEOUT_MS` (default `20000`) — page navigation timeout.
   - `SIGNAL_WAIT_TIMEOUT_MS` (default `8000`) — how long to wait for the
     primary stock signal (a JSON-LD `<script>` tag) to appear before
     proceeding anyway with whatever HTML rendered (fallback signals still
     run against it).
   - `PLAYWRIGHT_HEADLESS` (default `true`) — leave as default on Railway.
   - `PROXY_HOST`, `PROXY_PORT`, `PROXY_USERNAME`, `PROXY_PASSWORD` —
     Webshare (or any HTTP-auth proxy) credentials. **All optional** — with
     `PROXY_HOST`/`PROXY_PORT` unset, requests go out directly, so you can
     test this locally or on Railway before buying a proxy plan.

## HTTP surface

```
POST /check-stock
Body: {"url": "<product url>", "store": "iqoo" | "vivo"}
Response: {
  "url": "...", "store": "iqoo",
  "in_stock": true | false | null,   // null = check failed, see "signal"
  "signal": "JSON-LD offers.availability='https://schema.org/InStock'",
  "attempts": 1
}
```

```
POST /debug-network
Body: {"url": "<product url>", "pincode": "110001"}   // pincode optional
Response: {
  "url": "...", "pincode": "110001",
  "matched_requests": [
    {"url": "https://.../api/serviceability?pin=110001", "method": "GET",
     "status": 200, "body": "{\"serviceable\":true,...}"},
    ...
  ],
  "total_requests_seen": 12,     // every XHR/fetch response observed, for context
  "matched_count": 2,            // how many matched the capture keywords
  "all_responses_seen": [        // lightweight (no body) list of EVERY response,
    {"url": "...", "status": 200, "resource_type": "document"}, ...   // capped at 100
  ],
  "all_responses_truncated": false,
  "diagnostics": {
    "goto_status": 200, "goto_error": null,        // page.goto()'s own result/exception
    "final_url": "...", "page_title": "...",        // where navigation actually ended up
    "page_crashed": false,
    "networkidle_timed_out": false, "networkidle_error": null,
    "html_length": 45213, "html_snippet": "<!DOCTYPE html>...",
    "response_listener_errors": 0
  }
}
```
No auth on this endpoint (matches `/check-stock` — this whole service has
none, by design, since it's an internal pilot). Applies `pincode` as a
cookie named `pincode` on the target domain before navigating, then records
every XHR/fetch response whose URL contains `serviceability`, `delivery`,
`pincode`, `availability`, `stock`, or `fulfillment` (case-insensitive).
Built for `/debugreliance` on the main-bot side (RelianceDigital's stock
signal appears to live behind a pincode-gated API call rather than in the
page's own embedded JSON), but works against any URL.

**A live run against two real RelianceDigital URLs came back with
`total_requests_seen: 1` for both** — only the document itself, no
scripts/XHR at all, which pointed at either a silently swallowed
navigation error or an anti-bot challenge page being served instead of the
real site. Two things changed in response:
1. **Anti-detection measures** were added to every browser this service
   launches (not just this endpoint) — a realistic desktop Chrome
   user-agent, a normal 1280×800 viewport, and an init script patching the
   standard headless-Chromium tells (`navigator.webdriver`, `window.chrome`,
   `navigator.plugins`, `navigator.languages`). Vanilla headless Chromium is
   commonly fingerprinted and served a stripped-down page instead of the
   real one; this exact symptom (near-empty response, minimal further
   activity) was already confirmed and fixed the same way for
   `whatsapp_forwarder`'s WhatsApp Web automation.
2. **Every step that could silently fail is now caught and reported** in
   `diagnostics` instead of just producing a suspiciously low count with no
   explanation: `page.goto()`'s own error (if any) and HTTP status, the
   final URL after any redirects, the page title, whether the page crashed,
   whether the network-idle wait timed out or raised, and the first 500
   chars of whatever HTML actually loaded. `all_responses_seen` lists every
   response observed (not just keyword matches) so you can see exactly what
   *did* load even when nothing matched the capture keywords.

**The pincode-as-cookie approach is still a best-effort guess**, not a
confirmed mechanism — this sandbox has no live network access to check how
RelianceDigital's frontend actually reads a selected pincode (a cookie is
the most common convention, matching what the main bot's own quick-commerce
checkers already do, but it could instead be `localStorage`, a request
header, or something only set after a UI interaction like typing into a
pincode widget and clicking a button). If `matched_count` still comes back
0 despite `total_requests_seen` now being a realistic number, check
`all_responses_seen` for anything serviceability-shaped that just didn't
match the keyword list, and `diagnostics.page_title`/`html_snippet` for
signs of a login wall, captcha, or region-redirect page. Report back what's
actually observed so the approach can be adjusted, same as every other
live-tuning step this pilot has needed.

**Update: for RelianceDigital specifically, `diagnostics.goto_status` came
back `403` on both product URLs** — not a page-fingerprint issue the
anti-detection measures above could fix, but an Akamai WAF block on
Railway's outbound IP itself, at the network edge, before the page (real
or challenge) is even served. Direct-Playwright-from-Railway is a dead end
for this specific site; RelianceDigital checks have gone back to
Scrape.do (whose proxy pool, at least with `super=true`, gets past this
block — see the main bot's `/debugreliance <url> [pincode]` admin command
and `checkers/reliancedigital.py`). `/debug-network` itself is unchanged
and still useful for other sites/diagnostics that aren't behind an
IP-level WAF block like this one.

```
GET /health
Response: {"ok": true, "max_concurrent_checks": 2, "proxy_configured": false,
           "supported_stores": ["iqoo", "vivo"]}
```

Examples:
```bash
curl -X POST https://<your-service>.up.railway.app/check-stock \
  -H "Content-Type: application/json" \
  -d '{"url": "https://mshop.iqoo.com/in/product/...", "store": "iqoo"}'

curl -X POST https://<your-service>.up.railway.app/debug-network \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.reliancedigital.in/...", "pincode": "110001"}'
```

## Bandwidth optimization

Every request is intercepted (`page.route("**/*", ...)`) and only
`document`, `script`, `xhr`, and `fetch` resource types are allowed through
— images, fonts, stylesheets, and media are aborted before they download.
A product page's images alone can be several MB; since only `page.content()`
(the rendered DOM) is ever read, none of that is needed. Each check logs how
many requests were allowed vs. blocked, so the actual savings are visible in
the logs rather than assumed. `/debug-network` reuses this same filter —
`xhr`/`fetch` responses are exactly what it needs to inspect, so nothing
extra had to be allowed through for it.

## Stock detection — ported, not freshly reverse-engineered

This sandbox has no live network access to inspect real iQOO/Vivo product
pages. Rather than guess new selectors blind, `check_iqoo_vivo_stock()` in
`main.py` is **ported verbatim** from `checkers/iqoo.py` and
`checkers/vivo.py` in the main bot — both already probe-confirmed reliable
(a prior diagnostic pass tested real in-stock and out-of-stock URLs for
both stores): JSON-LD `offers.availability` is the primary signal, an
embedded-JSON stock key is a fallback, explicit "out of stock"/"sold out"
text is a last resort.

**This needs live verification once deployed** — the signal was proven
reliable when fetched via Scrape.do's `render=true`; it should behave the
same via Playwright (both fully execute the page's JS before reading the
DOM), but that's an assumption, not a confirmed fact from this environment.
Test both an in-stock and an out-of-stock URL for each store and compare
`/check-stock`'s result + `signal` field against ground truth before trusting
it for anything real.

One deliberate difference from the main bot's checkers: `checkers/iqoo.py`/
`vivo.py` default to `False` (out of stock) when no signal is found at all,
reasoning that a missed alert is safer than a false one in production. This
pilot instead returns `in_stock: null` ("check failed") in that case — since
this is a service being actively tuned, an inconclusive read should surface
for investigation rather than silently reporting "out of stock" as if it
were confident.

## Local testing (no proxy, no real site — HTTP layer + logic only)

```bash
cd playwright_scraper
pip install -r requirements.txt
python main.py
# in another terminal:
curl localhost:8080/health
```

For end-to-end testing against real iQOO/Vivo URLs, deploy to Railway (or
run locally with a real Chromium + network access) and hit `/check-stock`
directly with real product URLs — I cannot do this from the sandbox that
built this service.
