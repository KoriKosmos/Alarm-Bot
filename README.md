# Alarm Bot

A small Discord bot that DMs you at times you list in a config file.

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
cp .env.example .env    # then paste your token into it
```

Edit `config.yaml`:

```yaml
timezone: America/New_York
user_id: 123456789012345678

alarms:
  - time: "09:00"
    days: weekdays
    message: "Standup in 15 minutes."
```

- `user_id` — turn on **Settings → Advanced → Developer Mode**, then right-click yourself → **Copy User ID**.
- `timezone` — any IANA name (`America/New_York`, `Europe/London`, `Asia/Kolkata`). Alarm times are interpreted here, not in the host machine's timezone.
- `time` — 24-hour `HH:MM`.
- `days` — `everyday`, `weekdays`, `weekends`, or a list like `[mon, wed, fri]`.

**5. Check that DMs work, then run**

```bash
.venv/bin/python check_dm.py    # sends one test DM
.venv/bin/python bot.py
```

Run `check_dm.py` first. If it fails, the problem is step 2, not your alarm times.

## Behaviour worth knowing

- **Alarms missed while the bot was down are skipped, not backfilled.** Restarting at 09:00:30 will not re-send the 09:00 message. Say the word if you'd rather it catch up on startup.
- **Config is read once at startup.** Edit `config.yaml`, then restart the bot.
- **Bad config is fatal.** An unknown day name or malformed time exits with an error instead of silently never firing — a quiet alarm is indistinguishable from a working one until you miss something.
- **A failed send doesn't stop the bot.** Delivery errors are logged and the schedule keeps running.
- **Daylight saving:** times are matched against your local wall clock continuously, so an alarm inside a *repeated* hour (autumn fall-back) fires once rather than twice. The one gap is spring-forward — an alarm set inside the skipped hour (e.g. `02:30` in `America/New_York`) won't fire that day, because that wall-clock time doesn't exist.

## Keeping it running

The bot must stay running to fire. For an always-on setup, use a systemd user service:

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
| `bot.py` | The bot: config loading, validation, scheduling loop |
| `config.yaml` | Your alarms — safe to commit |
| `check_dm.py` | One-shot test that DMs actually get through |
| `.env` | Your bot token — **never commit** (already gitignored) |
