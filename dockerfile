# Ye Playwright aur Chromium ka official pre-built image hai
FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

# Container ke andar working directory set kar rahe hain
WORKDIR /app

# Aapka saara code container mein copy karega
COPY . /app

# Requirements install karega
RUN pip install --no-cache-dir -r requirements.txt

# Bot ko start karne ki command
CMD ["python", "bot.py"]
