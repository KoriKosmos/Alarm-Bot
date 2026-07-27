FROM python:3.13-slim

# git   — the entrypoint pulls the latest code on every container start.
# tzdata — pinned explicitly rather than relied on. The current base image
#          happens to carry the tz database, but zoneinfo needs it to resolve
#          names like America/Los_Angeles, and without it every config fails
#          validation with a misleading "not an IANA name".
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies are baked in at build time, so a restart never depends on PyPI
# being reachable. Changing requirements.txt therefore needs a rebuild
# (./update-deploy.sh), not just a restart.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV REPO_URL=https://github.com/KoriKosmos/Alarm-Bot.git \
    BRANCH=main \
    APP_DIR=/app/src \
    ALARM_CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
