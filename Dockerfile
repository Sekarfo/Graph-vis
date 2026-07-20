FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PORT=8080

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
	&& pip install --no-cache-dir -r requirements.txt

COPY server.py progress_core.py matcher.py graph-app.js graph-data.js index.html zhambyl-graph.json entrypoint.py ./

RUN useradd --create-home --home-dir /home/app --shell /usr/sbin/nologin appuser \
	&& chown -R appuser:appuser /app

# Контейнер стартует от root: entrypoint.py должен успеть chown'нуть
# смонтированный DATA_DIR (volume монтируется от root) перед тем, как сам
# уронит привилегии до appuser и запустит uvicorn. Без USER appuser здесь —
# см. entrypoint.py.
EXPOSE 8080

CMD ["python3", "entrypoint.py"]
