FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=80

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY import_app.py .
COPY metadata_app.py .
COPY metadata_hotfix.py .
COPY archive_sync_app.py .

EXPOSE 80

CMD ["sh", "-c", "uvicorn archive_sync_app:app --host 0.0.0.0 --port ${PORT:-80} --workers 1"]
