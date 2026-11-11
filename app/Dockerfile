FROM python:3.12-alpine AS base
WORKDIR /opt/rag
ENV PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml README.md requirements.lock ./
COPY rag ./rag
RUN pip install --no-cache-dir -c requirements.lock .
USER 10001:10001
HEALTHCHECK --interval=5s --timeout=4s --retries=5 CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/ready', timeout=3).close()"]

FROM base AS core
CMD ["python", "-m", "rag", "core"]

FROM base AS worker
USER root
RUN mkdir -p /var/lib/rag/spool && chown 10001:10001 /var/lib/rag/spool
USER 10001:10001
CMD ["python", "-m", "rag", "worker"]
