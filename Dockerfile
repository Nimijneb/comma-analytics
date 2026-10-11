FROM python:3.12-slim
# APP_UID/APP_GID match the host user that owns ./archive (setup.py fills them in)
ARG APP_UID=1000
ARG APP_GID=1000
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client rsync && rm -rf /var/lib/apt/lists/* \
 && groupadd -g ${APP_GID} app && useradd -u ${APP_UID} -g ${APP_GID} -m app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY schema/ schema/
COPY ct/ ct/
COPY sync.py setup_grafana.py ./
ENV PYTHONUNBUFFERED=1 ARCHIVE_DIR=/app/archive
USER app
CMD ["python", "sync.py", "--daemon"]
