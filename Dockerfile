FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir aiohttp websockets
COPY scanner_v2.py squeeze_report.py config.json docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh && mkdir -p /data/reports
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/squeeze.db REPORTS_DIR=/data/reports SYMBOLS_CACHE=/data/symbols_cache.json
EXPOSE 8000
CMD ["./docker-entrypoint.sh"]
