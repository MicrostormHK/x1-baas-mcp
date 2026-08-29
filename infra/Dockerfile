FROM python:3.12-slim

# Install Node.js (required for Readability.js)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# System deps for Camoufox (Firefox requirements)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libatk1.0-0 \
    libatk-bridge2.0-0 libdrm2 libxshmfence1 \
    fonts-liberation libfreetype6 libfontconfig1 \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY infra/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fetch Camoufox stealth browser binaries
RUN camoufox fetch

# Copy application source
COPY src/ ./src/
COPY mcp/ ./mcp/

# Copy env template
COPY infra/.env.example .env

# Expose ports: 8000=REST API, 8001=MCP server
EXPOSE 8000 8001

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://localhost:8000/health'); assert r.status_code==200"

# Default: run REST API server
CMD ["python", "src/server.py"]
