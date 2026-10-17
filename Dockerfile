FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser

COPY deliverytwin ./deliverytwin

USER appuser
EXPOSE 8000

CMD ["python", "-m", "deliverytwin", "--db", "/tmp/deliverytwin.db", "serve", "--host", "0.0.0.0", "--port", "8000"]
