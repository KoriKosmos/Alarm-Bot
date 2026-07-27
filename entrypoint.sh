#!/bin/sh
# Fetches the latest code from the repo, then runs the bot. Because this happens
# on every container start, deploying a code change is just:
#
#     docker compose restart
#
set -e

REPO_URL="${REPO_URL:-https://github.com/KoriKosmos/Alarm-Bot.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/app/src}"

if [ -d "$APP_DIR/.git" ]; then
    echo "==> updating from $REPO_URL ($BRANCH)"
    # Deliberately not fatal. If GitHub is unreachable at start, running the
    # code already on disk beats not running at all — a bot that silently
    # stops alarming is the worst outcome here.
    if git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH" \
        && git -C "$APP_DIR" reset --hard "origin/$BRANCH"; then
        echo "==> now at $(git -C "$APP_DIR" rev-parse --short HEAD)"
    else
        echo "WARN: update failed, starting the code already on disk" >&2
    fi
else
    # The only case with no fallback, so this one is allowed to be fatal.
    # The repo is public, so no credentials are needed.
    echo "==> first run, cloning $REPO_URL ($BRANCH)"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

exec python "$APP_DIR/bot.py"
