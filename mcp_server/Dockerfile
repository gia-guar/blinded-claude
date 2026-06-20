FROM python:3.11-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY mcp_server/requirements.txt /app/mcp_server/requirements.txt
RUN pip install --no-cache-dir -r /app/mcp_server/requirements.txt

# Server code + the isolated pipeline runner. The kedro project (incl. data) is
# bind-mounted at runtime via docker-compose, and made importable through
# PYTHONPATH below.
COPY mcp_server/server.py /app/mcp_server/server.py
COPY mcp_server/pipeline_runner.py /app/mcp_server/pipeline_runner.py

ENV KEDRO_PROJECT_PATH=/app/kedro_project
ENV PYTHONPATH=/app/kedro_project/src
ENV PYTHONUNBUFFERED=1
# Play nicely with a read-only filesystem (see docker-compose.yml):
ENV PYTHONDONTWRITEBYTECODE=1
ENV DO_NOT_TRACK=1

EXPOSE 8000
CMD ["python", "/app/mcp_server/server.py"]
