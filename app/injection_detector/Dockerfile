# Сервис детектора prompt injection: docs/documents/model-gateway/injection-detector.md.
# Debian, а не Alpine: для musl нет сборок ONNX Runtime.
FROM python:3.12-slim
WORKDIR /opt/detector
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DETECTOR_MODEL_DIR=/opt/detector/model
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Веса входят в образ: закреплённая ревизия со сверкой SHA-256, при запуске сеть не нужна.
# Слой с весами зависит только от model_files.py и не пересобирается при правке сервера.
COPY model_files.py ./
RUN python model_files.py
COPY server.py ./
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=4s --retries=12 --start-period=60s CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/health/ready', timeout=3).close()"]
CMD ["python", "server.py"]
