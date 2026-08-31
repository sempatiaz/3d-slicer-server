FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Debian deposundaki PrusaSlicer gerçek CLI entegrasyonudur. Paket mimaride
# bulunamazsa build bilinçli olarak başarısız olur; sessiz placeholder üretmez.
RUN apt-get update \
    && apt-get install -y --no-install-recommends prusa-slicer xvfb ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY profiles ./profiles
COPY start.sh ./start.sh
RUN chmod +x ./start.sh && useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 10000
CMD ["./start.sh"]
