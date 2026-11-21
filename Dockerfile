FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=3000 \
    DATA_DIR=/app/data \
    UPLOAD_DIR=/app/uploads

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/data /app/uploads \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 3000
CMD ["sh", "-c", "exec python -m uvicorn app:app --host 0.0.0.0 --port \"${PORT:-3000}\" --proxy-headers"]
