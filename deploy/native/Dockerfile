FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/var/lib/native HF_HOME=/var/lib/native/hf \
    XDG_CACHE_HOME=/var/lib/native/cache HF_HUB_DISABLE_TELEMETRY=1 PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/native
RUN pip install --no-cache-dir torch==2.9.0 --index-url https://download.pytorch.org/whl/cpu
COPY deploy/native/requirements.txt ./requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 120 --retries 8 -r requirements.txt \
    && mkdir -p /var/lib/native && chown -R 10001:10001 /var/lib/native
COPY deploy/native/*.py ./
COPY deploy/model-profile.json ./profile.json
USER 10001:10001
ENTRYPOINT ["python", "server.py"]
