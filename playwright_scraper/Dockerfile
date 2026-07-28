# Playwright's official image ships Chromium plus every system library it
# needs already installed — same reasoning as whatsapp_forwarder/Dockerfile:
# Railway's default nixpacks Python builder does not, and fighting Playwright
# onto it isn't worth it. Deploy this service with builder = "Dockerfile"
# (root directory playwright_scraper/), same as whatsapp_forwarder.
#
# Version pin history: this sat at v1.47.0 (Chromium 129, released ~Sept
# 2024) unchanged since this service was first built, while
# checkers/apple.py's own User-Agent for Apple's fulfillment-messages
# endpoint had drifted to claiming a MUCH newer browser (Chrome/Edge 150)
# from a real capture — a growing, detectable engine-vs-claimed-identity
# mismatch (TLS/JA3 fingerprint, HTTP/2 behavior, and JS feature surface
# all still reflect the real frozen Chromium 129 engine no matter what the
# User-Agent header claims). Bumped to v1.60.0 (Chromium ~131) as part of
# investigating Apple's fulfillment-messages 541s; see
# playwright_scraper/main.py's _apple_user_agent_for, which now DERIVES
# the claimed Chrome/Edge version from this image's actual bundled
# browser at runtime instead of a hardcoded string — so this pin can be
# bumped again in the future without also having to remember to update a
# separate hardcoded version number to match.
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# xvfb (2026-07-28): _check_pickup_availability (see main.py's
# APPLE_PICKUP_HEADLESS) needs headless=False — a controlled same-network
# A/B test confirmed headless Chromium fails to render Apple's
# pickUpDetails widget at all, while an otherwise-identical headful run
# succeeds consistently. Railway's container has no physical display,
# so a real (non-headless) Chromium launch has nothing to render into
# without one. xvfb-run below wraps CMD to start a virtual X server and
# point Chromium at it, so "headful" launches work here too. This is a
# genuinely different code path from Chromium's own --headless flag —
# Xvfb just gives a real display server for the browser's normal
# (non-headless) rendering pipeline to use, which is the actual thing
# that was confirmed to matter, not merely a workaround for a missing
# screen. Every OTHER endpoint in this service stays headless by default
# (see HEADLESS) and is unaffected — Xvfb is a no-op wrapper for a
# headless launch, not a requirement for it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# No volume needed — this service is fully stateless (no login/session to
# persist between requests, unlike whatsapp_forwarder).
#
# --auto-servernum: picks an unused X display number rather than assuming
# :99 is free (matters if this container ever runs more than one Xvfb
# process). Screen size 1280x800 matches _new_browser_and_context's own
# viewport exactly, so nothing is rendered outside the visible virtual
# screen. -ac disables X access control inside the container's own
# isolated display — nothing outside this container can reach it anyway.
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1280x800x24 -ac", "python", "main.py"]
