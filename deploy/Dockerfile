FROM python:3.12-slim-bookworm
WORKDIR /opt/rag
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RAG_ENV_FILE=/nonexistent/deployment.env
COPY app/pyproject.toml app/README.md app/requirements.lock ./
COPY app/rag ./rag
COPY deploy/requirements.txt ./deployment-requirements.txt
RUN pip install --no-cache-dir -c requirements.lock . -r deployment-requirements.txt \
    && mkdir -p /var/lib/rag/spool /var/lib/rag/models \
    && chown -R 10001:10001 /var/lib/rag
COPY deploy/*.py ./deploy/
COPY deploy/monitoring ./deploy/monitoring/
USER 10001:10001
CMD ["python", "-m", "deploy", "core"]
