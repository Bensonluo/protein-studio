FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROTEIN_STUDIO_HOST=0.0.0.0 \
    PROTEIN_STUDIO_PORT=8765 \
    PROTEIN_STUDIO_DATA_DIR=/app/data \
    PROTEIN_STUDIO_WORKERS=1

RUN groupadd --gid 10001 studio \
    && useradd --uid 10001 --gid studio --no-create-home studio \
    && mkdir -p /app/data \
    && chown studio:studio /app/data

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=studio:studio backend ./backend
COPY --chown=studio:studio web ./web
COPY --chown=studio:studio fixtures ./fixtures
COPY --chown=studio:studio config ./config
COPY --chown=studio:studio scripts ./scripts
COPY --chown=studio:studio sources.json ./sources.json

USER studio
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:8765' + os.environ.get('PROTEIN_STUDIO_BASE_PATH', '') + '/api/health', timeout=3).read()"
CMD ["python", "-m", "backend.server"]
