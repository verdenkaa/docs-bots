# Server packaging reuses the existing Horizon ONNX implementation unchanged.
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DETECTOR_MODEL_DIR=/var/lib/detector
WORKDIR /opt/detector
COPY app/injection_detector/requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 120 --retries 8 -r requirements.txt \
    && mkdir -p /var/lib/detector && chown -R 10001:10001 /var/lib/detector
COPY app/injection_detector/model_files.py app/injection_detector/server.py ./
USER 10001:10001
CMD ["sh", "-ec", "python model_files.py && exec python server.py"]
