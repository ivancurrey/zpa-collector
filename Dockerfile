# zpa-collector — single, stdlib-only, zero-pip image.
FROM python:3.11-slim

# Non-root runtime user; owns the data volume mountpoint.
RUN useradd --create-home --uid 10001 collector \
    && mkdir -p /data \
    && chown collector:collector /data

WORKDIR /app

# Runtime is standard library only — no pip install step, no requirements.
COPY collector/ /app/collector/

ENV PYTHONUNBUFFERED=1 \
    HTTP_PORT=8866 \
    LSS_PORT=4639 \
    DB_PATH=/data/state.db \
    LSS_CERT_PATH=/data/receiver.crt \
    LSS_KEY_PATH=/data/receiver.key

USER collector

EXPOSE 4639 8866

# Curl-less healthcheck: stdlib urllib against /health. Non-zero exit = unhealthy
# (/health returns non-200 when DEGRADED/AT-RISK).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/health' % os.environ.get('HTTP_PORT','8866'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).getcode()==200 else 1)" \
    || exit 1

CMD ["python", "-m", "collector"]
