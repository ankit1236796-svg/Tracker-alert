# Playwright's official image ships Chromium plus every system library it
# needs (libnss3, libatk, etc.) already installed — trying to get those onto
# Railway's default nixpacks Python builder is a losing battle, so this
# service deploys as a Dockerfile-built Railway service instead (set the
# builder to "Dockerfile" for this service, root directory whatsapp_forwarder/).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# The browser profile + debug screenshots are meant to live on a volume
# mounted at /data — WITHOUT one, every redeploy loses the WhatsApp login
# and a fresh QR scan is needed. Deliberately no Dockerfile VOLUME
# instruction here: Railway rejects it at build time ("Invalid docker
# volume") since it attaches volumes through its own service config/UI,
# not native Docker volumes — attach the volume at /data there instead.
# See README.md.

CMD ["python", "main.py"]
