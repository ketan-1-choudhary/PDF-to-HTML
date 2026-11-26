# Base image with Python
FROM python:3.11-slim

# Install Node.js (and npm) alongside Python
RUN apt-get update && \
        apt-get install -y --no-install-recommends \
            curl \
            gnupg \
            chromium \
            poppler-utils \
            tesseract-ocr \
            tesseract-ocr-eng \
            tesseract-ocr-ita \
            jq \
            fontconfig \
            fonts-dejavu \
            fonts-liberation \
            libpango-1.0-0 \
            libpangocairo-1.0-0 \
            libcairo2 \
            libgdk-pixbuf-2.0-0 \
            libffi8 && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies (expects a requirements.txt in the project root)
COPY requirements.txt ./
# Pre-install CPU-only torch so docling doesn't pull the multi-GB CUDA build.
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Claude Code refuses bypassPermissions when run as root.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin worker
COPY --chown=worker:worker . .

ENV HOME=/home/worker \
    PROJECT_ROOT=/app \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

RUN npm ci --prefix /app/.claude/skills/pdf-to-html/scripts --omit=dev --no-audit --no-fund

USER worker

# Run the Python code (adjust the entrypoint filename as needed)
CMD ["python", "run_skill.py"]