#!/bin/sh
# Full redeploy: rebuilds the image as well as updating the code.
#
# You only need this when the image itself changes — requirements.txt,
# Dockerfile, entrypoint.sh or docker-compose.yml. For an ordinary code change,
# `docker compose restart` is enough, since the container re-pulls the repo
# every time it starts.
set -e

echo "🚀 Deploying updates..."

echo "📥 Pulling from git..."
git pull

echo "🐳 Rebuilding and restarting Docker containers..."
docker compose up -d --build --remove-orphans --force-recreate

docker image prune -f

echo "✅ Deployment complete! Bot is running."
echo "   Logs: docker compose logs -f"
