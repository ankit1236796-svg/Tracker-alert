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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# No volume needed — this service is fully stateless (no login/session to
# persist between requests, unlike whatsapp_forwarder).
CMD ["python", "main.py"]
