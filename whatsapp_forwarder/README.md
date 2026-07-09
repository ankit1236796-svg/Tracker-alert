# whatsapp_forwarder

Separate service that owns the WhatsApp Web browser automation for the
per-user "back in stock" forwarding feature. Runs as its own Railway
service, its own container — completely separate from the main
Tracker-alert bot process. If this service is down, slow, or its WhatsApp
session gets logged out, the main bot's Telegram alerts are **not**
affected; they just stop also landing on WhatsApp until this is fixed.

## Why a separate service

- The main bot has zero Playwright dependency. A crash or hang in browser
  automation here can't destabilize Telegram alerts, deduplication, the
  dashboard, or anything else already working.
- One WhatsApp account (a dedicated number you're willing to risk — this
  uses unofficial WhatsApp Web automation and carries a ban risk) logs in
  once here, then posts into **many different users' own Channels or
  Communities**, one invite link per user. Each user registers their
  channel via `/setwhatsapp` on the main bot; you (the admin) join it with
  this same WhatsApp account and approve it before forwarding starts —
  see `/whatsapppending`, `/whatsappapprove`, `/whatsappdisable` on the
  main bot, or the dashboard's "WhatsApp" page.

## Deploying on Railway

1. In your Railway project, **New Service → GitHub Repo** → same repo,
   but set **Root Directory** to `whatsapp_forwarder/`.
2. Set the service's **Builder** to **Dockerfile** (this directory ships
   its own `Dockerfile` based on Playwright's official image, which
   already has Chromium and all its system libraries installed — Railway's
   default nixpacks Python builder does not, and getting Playwright
   working on it is not worth fighting).
3. **Attach a volume** mounted at `/data`. This is where the browser
   profile (and therefore the logged-in WhatsApp session) and failure
   debug screenshots live. Without this, every redeploy logs you out and
   needs a fresh QR scan.
4. Set environment variables on this service:
   - `WHATSAPP_FORWARDER_SECRET` — any random string; must match the
     **same** value set as `WHATSAPP_FORWARDER_SECRET` on the main bot
     service.
   - `BOT_TOKEN` — the same Telegram bot token the main bot uses. Reused
     here only to DM you (the admin) the login QR code and failure
     screenshots — no aiogram dependency, just a direct HTTP call.
   - `ADMIN_USER_ID` — your Telegram user id (same value as the main
     bot's `ADMIN_USER_ID`), i.e. where the QR photo gets sent.
   - `WHATSAPP_PROFILE_DIR` — optional, defaults to
     `/data/whatsapp-profile`. Leave as default if your volume is
     mounted at `/data`.
   - `WHATSAPP_HEADLESS` — optional, defaults to `true`. Leave it true on
     Railway (no display anyway).
   - `WHATSAPP_FORWARD_PACING_SECONDS` — optional, defaults to `12`.
     Delay between consecutive queued sends — a deliberate throttle to
     look less automated. Raise it if you see any WhatsApp warnings.
5. On the **main bot service**, set:
   - `WHATSAPP_FORWARDER_URL` — this service's public Railway URL, no
     trailing slash (e.g. `https://whatsapp-forwarder-production.up.railway.app`).
   - `WHATSAPP_FORWARDER_SECRET` — same value as step 4.

   The main bot treats an unset `WHATSAPP_FORWARDER_URL` as "feature
   disabled" — it makes zero network calls to this service and behaves
   identically to before this feature existed. Nothing here goes live
   until both env vars are set on the main bot.
6. Deploy this service. Watch its logs — on first boot (no saved
   session yet) it will detect it isn't logged in and DM you a QR code
   on Telegram (via `BOT_TOKEN`/`ADMIN_USER_ID`). Open WhatsApp on the
   phone/number you're dedicating to this → **Linked Devices** → **Link a
   Device** → scan it. The photo refreshes automatically roughly every
   20 seconds until scanned or a ~10 minute timeout (configurable via
   `WHATSAPP_QR_TIMEOUT_SECONDS`); after a timeout it keeps checking at a
   relaxed pace and re-sends fresh QR photos the next time it has reason
   to check the login state.

## This WILL need live iteration — I cannot verify it from here

I built and unit-tested the HTTP layer (`/forward`, `/status`, auth,
validation, queueing) with a mocked Flask test client, and confirmed
Playwright's `launch_persistent_context` + navigation mechanics work in
this sandbox. What I **cannot** do from here is drive a real WhatsApp Web
session — no live network access to `web.whatsapp.com`. The CSS selectors
in `main.py` (QR canvas, the logged-in chat-list marker, the message
input box, the send button, the "Continue to Chat" interstitial some
invite links show) are best-effort based on WhatsApp Web's general
structure, but WhatsApp changes its DOM/class names often and these are
**not guaranteed correct** — they need to be checked against the live
site.

If a step fails, the worker:
- logs exactly which selector list it tried and that none matched,
- saves a screenshot of the page at the moment of failure, and
- sends that screenshot to you on Telegram automatically (same as the QR
  bootstrap), so you can see exactly what the page looked like without
  needing shell/volume access to the Railway container.

To fix a broken selector: open `web.whatsapp.com` yourself in a normal
browser, inspect the relevant element, and add/replace the matching entry
in the selector lists near the top of `main.py` (`_QR_CANVAS_SELECTORS`,
`_LOGGED_IN_SELECTORS`, `_CONTINUE_TO_CHAT_SELECTORS`,
`_MESSAGE_BOX_SELECTORS`, `_SEND_BUTTON_SELECTORS`) — each is an ordered
list of candidates, so add new ones rather than replacing wholesale, and
the most likely-correct one first.

## WhatsApp Channels vs Communities — an important distinction

Only a Channel's **admin** can post into it (followers can't message it —
it's broadcast-only). For a user's Channel to actually receive forwarded
alerts, you (running this dedicated WhatsApp account) must be added as an
**admin** of their Channel, not just a follower — this is a manual step
the user does before their registration can be usefully approved. For
Communities/Groups (`chat.whatsapp.com/...` links), being a regular
member is enough to send messages.

## Local testing (HTTP layer only, no real WhatsApp)

```bash
cd whatsapp_forwarder
pip install -r requirements.txt
WHATSAPP_FORWARDER_SECRET=test BOT_TOKEN= ADMIN_USER_ID= python main.py
```

This starts the Flask server and the worker thread (which will try to
launch a real browser and navigate to WhatsApp Web — expect it to hang at
the QR step without `BOT_TOKEN`/`ADMIN_USER_ID` set to actually see the
QR). To test just the HTTP surface without touching a browser at all,
import `create_app()` from `main.py` in a script and hit it with Flask's
test client, the way the main repo's synthetic tests do for `dashboard.py`.
