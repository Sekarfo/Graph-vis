FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PORT=8080

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
	&& pip install --no-cache-dir -r requirements.txt

COPY server.py progress_core.py matcher.py schema.py graph-app.js index.html entrypoint.py ./
# Графы (по областям) — сервер сам находит их сканированием regions/
# (см. discover_graphs() в server.py): новая область появляется в образе
# просто новым файлом, Dockerfile трогать не нужно.
COPY regions/ ./regions/

RUN useradd --create-home --home-dir /home/app --shell /usr/sbin/nologin appuser \
	&& chown -R appuser:appuser /app

# Контейнер стартует от root: entrypoint.py должен успеть chown'нуть
# смонтированный DATA_DIR (volume монтируется от root) перед тем, как сам
# уронит привилегии до appuser и запустит uvicorn. Без USER appuser здесь —
# см. entrypoint.py.
EXPOSE 8080

CMD ["python3", "entrypoint.py"]
