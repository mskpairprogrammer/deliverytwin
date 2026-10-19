FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DELIVERYTWIN_DEMO_MODE=0

# The server has no built-in authentication and refuses to bind 0.0.0.0 unless the
# deployment sets DELIVERYTWIN_TRUSTED_AUTH=1 to confirm an authenticating gateway
# (Entra ID, App Service auth, Databricks Apps) sits in front of it.

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser

COPY deliverytwin ./deliverytwin

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["python", "-m", "deliverytwin", "--db", "/tmp/deliverytwin.duckdb", "serve", "--host", "0.0.0.0", "--port", "8000"]
