# Alarm Bot

A small Discord bot that DMs one or more people at times you list in a config file.

## Setup

**1. Create the bot and get a token**

Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → **Bot** → **Reset Token**, and copy the token.

You do *not* need to enable any Privileged Gateway Intents. Schedules come from `config.yaml`, so the bot never reads messages. (This is the step people most often waste time on.)

**2. Let the bot reach you**

Discord will not let a bot DM someone it has no connection to. Under **OAuth2 → URL Generator**, tick `bot`, open the generated URL, and invite the bot to any server you are also in. Then check **User Settings → Content & Social → Allow direct messages from server members**.

**3. Install**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**4. Configure**

```bash
cp .env.example .env                  # then paste your token into it
cp config.example.yaml config.yaml    # then edit your alarms
```

Both `.env` and `config.yaml` are gitignored, so your token, Discord user IDs, and personal alarms stay out of the repo. Edit `config.yaml`:

```yaml
timezone: America/New_York

users:
  me: 123456789012345678
  partner: 987654321098765432

alarms:
  - time: "09:00"
    days: weekdays
    message: "Standup in 15 minutes."
    to: me

  - time: "20:00"
    days: everyday
    message: "Time for meds."
    to: [me, partner]
```

- `users` — a name for each person, mapped to their Discord user ID. Names are yours to pick; only `to` uses them. To get an ID, turn on **Settings → Advanced → Developer Mode**, then right-click a user → **Copy User ID**.
- `timezone` — any IANA name (`America/New_York`, `Europe/London`, `Asia/Kolkata`). Alarm times are interpreted here, not in the host machine's timezone.
- `time` — 24-hour `HH:MM`. **Keep the quotes** — unquoted, YAML reads `21:30` as the number 1290.
- `days` — `everyday`, `weekdays`, `weekends`, or a list like `[mon, wed, fri]`.
- `to` — one name or a list of them. Optional while exactly one user is defined; required past that, so adding someone never signs them up for every existing alarm by accident.

Everyone listed under `users` must separately share a server with the bot and allow DMs, exactly as in step 2.

**5. Check that DMs work, then run**

```bash
.venv/bin/python check_dm.py    # sends one test DM per user
.venv/bin/python bot.py
```

Run `check_dm.py` first — it reports per user, so you can see at a glance who the bot can't reach. If it fails, the problem is step 2, not your alarm times.

### Upgrading from the single-user format

The old `user_id: 123` format still works, so pulling this version won't stop your alarms. You'll see a warning on startup until you convert:

```bash
.venv/bin/python migrate_config.py --dry-run   # preview, changes nothing
.venv/bin/python migrate_config.py             # rewrite it
```

It backs the original up to `config.yaml.bak` (never overwriting an earlier backup), writes atomically, and re-loads the result before declaring success — rolling back if the bot would reject it. Running it twice is a no-op, and it refuses to touch a config that is already invalid. The running bot picks the new file up on its next tick, with no restart and no re-sending of anything already delivered today.

On the server the compose mount is read-only, so run it against the host copy from the directory holding `config.yaml`:

```bash
docker run --rm -v "$PWD:/work" -w /work --entrypoint python alarm-bot-bot migrate_config.py
```

## Behaviour worth knowing

- **Alarms missed while the bot was down are skipped, not backfilled.** Restarting at 09:00:30 will not re-send the 09:00 message. Say the word if you'd rather it catch up on startup.
- **`config.yaml` reloads live.** Save the file and the running bot picks up added, edited, and removed alarms within ~20 seconds. No restart, no `.env` reload (the token is only read at startup).
- **A bad edit never takes down a running bot.** At startup an unknown day name or malformed time is fatal, so you find out immediately. After startup the same mistake is only logged, and the bot keeps running on the last good config — a typo saved mid-edit shouldn't silently kill every future alarm. Watch the log to confirm an edit was accepted; you'll see either `reloaded config.yaml: N alarm(s)` or the error. Deleting the file is likewise survivable.
- **Editing won't re-send an alarm that already went out today.** Alarms are tracked by time, message and recipient rather than list position, so inserting, reordering, renaming a user or migrating the file doesn't cause a repeat.
- **One unreachable person doesn't affect anyone else.** Each recipient of an alarm is delivered and tracked independently, so a blocked DM costs only that person their message.
- **A failed send doesn't stop the bot.** Delivery errors are logged and the schedule keeps running.
- **Daylight saving:** times are matched against your local wall clock continuously, so an alarm inside a *repeated* hour (autumn fall-back) fires once rather than twice. The one gap is spring-forward — an alarm set inside the skipped hour (e.g. `02:30` in `America/New_York`) won't fire that day, because that wall-clock time doesn't exist.

## Running on a server (Docker)

The bot must stay running to fire. On the server:

```bash
git clone https://github.com/KoriKosmos/Alarm-Bot.git
cd Alarm-Bot
cp .env.example .env                  # paste your bot token
cp config.example.yaml config.yaml    # set users, timezone, alarms
docker compose up -d
docker compose logs -f
```

Both `.env` and `config.yaml` are gitignored, so a fresh clone has neither — you must create them on the server before the first start, or the container will exit with a config error.

### Deploying updates

The container pulls the repo itself every time it starts, so shipping a code change is:

```bash
docker compose restart
```

That's the normal path — no rebuild, no `git pull` on the server. Because the repo is public, the container needs no credentials to do it.

You only need the full redeploy when the *image* changes — `requirements.txt`, `Dockerfile`, `entrypoint.sh`, or `docker-compose.yml`:

```bash
./update-deploy.sh
```

### How it fits together

- **Code** is cloned into a named volume at container start, not baked into the image. A failed fetch (GitHub down) is logged as a warning and the bot starts on the last code it pulled, rather than not starting at all.
- **Dependencies** are baked in at build time, so a restart never depends on PyPI being reachable. Changing `requirements.txt` therefore needs `./update-deploy.sh`.
- **`config.yaml` stays on the host**, mounted read-only, and is found via the `ALARM_CONFIG` env var. It sits outside the code tree so the start-up `git reset --hard` can't touch it. Editing it on the host is picked up live by the running bot — no restart.
- **Restarts don't backfill.** An alarm whose time falls inside a redeploy window is skipped, same as any other restart.

### systemd instead

If you'd rather not use Docker, a user service works too — but note it won't self-update:

```ini
# ~/.config/systemd/user/alarm-bot.service
[Unit]
Description=Discord alarm bot

[Service]
WorkingDirectory=%h/GitHub/Alarm-Bot
ExecStart=%h/GitHub/Alarm-Bot/.venv/bin/python bot.py
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now alarm-bot
journalctl --user -u alarm-bot -f
```

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | The bot: config loading, validation, live reload, scheduling loop |
| `config.example.yaml` | Template to copy — the version that is committed |
| `config.yaml` | Your alarms — **gitignored**, reloaded live while running |
| `check_dm.py` | One-shot test that DMs get through, per user |
| `migrate_config.py` | Converts an old single-user config to the `users:` format |
| `.env` | Your bot token — **never commit** (already gitignored) |
| `Dockerfile` | Image: Python + git + baked dependencies |
| `entrypoint.sh` | Pulls the latest code, then starts the bot |
| `docker-compose.yml` | Server deployment: env, config mount, restart policy |
| `update-deploy.sh` | Full redeploy for when the image itself changes |
