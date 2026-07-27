"""Discord alarm bot: DMs one user at times listed in config.yaml."""

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
import yaml
from discord.ext import tasks
from dotenv import load_dotenv

# Defaults to config.yaml beside this file. ALARM_CONFIG overrides it, which is
# what the container uses: the code tree is re-cloned on every start, so config
# has to live outside it to survive.
CONFIG_PATH = os.environ.get("ALARM_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.yaml"
)

# How often to check the clock. Deliberately faster than the one-minute
# resolution we match on, so a tick landing at :59.6 can't skip a minute.
# The fired-set below stops the extra ticks from double-sending.
TICK_SECONDS = 20

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_ALIASES = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu",
    "friday": "fri", "saturday": "sat", "sunday": "sun",
}
DAY_GROUPS = {
    "everyday": DAY_NAMES,
    "daily": DAY_NAMES,
    "all": DAY_NAMES,
    "weekdays": DAY_NAMES[:5],
    "weekends": DAY_NAMES[5:],
}

log = logging.getLogger("alarm-bot")


class ConfigError(Exception):
    """Raised when config.yaml is malformed. Always fatal at startup."""


def parse_days(raw, where):
    """Normalise an alarm's `days` value into a set of short day names."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where}: 'days' must be a name or a non-empty list")

    days = set()
    for entry in raw:
        key = str(entry).strip().lower()
        if key in DAY_GROUPS:
            days.update(DAY_GROUPS[key])
        elif key in DAY_ALIASES:
            days.add(DAY_ALIASES[key])
        elif key in DAY_NAMES:
            days.add(key)
        else:
            raise ConfigError(
                f"{where}: unknown day {entry!r}. Use one of "
                f"{', '.join(DAY_NAMES)}, or everyday/weekdays/weekends"
            )
    return days


def parse_time(raw, where):
    """Validate a 24-hour HH:MM string and return it zero-padded."""
    text = str(raw).strip()
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ConfigError(
            f"{where}: 'time' must be 24-hour HH:MM (e.g. 09:00 or 22:30), got {raw!r}"
        ) from None


def config_signature(path=CONFIG_PATH):
    """Cheap fingerprint used to notice edits. Returns None if the file is gone.

    mtime is nanosecond-resolution and paired with size, so two saves in the same
    second still register as a change.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def load_config(path=CONFIG_PATH):
    """Read and fully validate config.yaml. Anything wrong raises ConfigError.

    Validation is loud on purpose: an alarm that silently never fires looks
    identical to a working bot until you miss the thing it was reminding you about.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        raise ConfigError(
            f"no config file at {path}. Create one with: cp config.example.yaml config.yaml"
        ) from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a top-level mapping")

    try:
        tz = ZoneInfo(str(data.get("timezone", "")))
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"'timezone' must be an IANA name like America/New_York, "
            f"got {data.get('timezone')!r}"
        ) from None

    try:
        user_id = int(data["user_id"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError("'user_id' must be a Discord user ID (a long number)") from None
    if user_id <= 0:
        raise ConfigError("'user_id' is still the placeholder — set your real Discord user ID")

    raw_alarms = data.get("alarms")
    if not isinstance(raw_alarms, list) or not raw_alarms:
        raise ConfigError("'alarms' must be a non-empty list")

    alarms = []
    for index, entry in enumerate(raw_alarms):
        where = f"alarms[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: each alarm must be a mapping")
        message = entry.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ConfigError(f"{where}: 'message' must be a non-empty string")
        alarms.append({
            "index": index,
            "time": parse_time(entry.get("time"), where),
            "days": parse_days(entry.get("days"), where),
            "message": message.strip(),
        })

    return {"tz": tz, "user_id": user_id, "alarms": alarms}


class AlarmBot(discord.Client):
    def __init__(self, config):
        # No privileged intents: schedules come from the config file, so the
        # bot never reads message content and needs nothing toggled in the portal.
        super().__init__(intents=discord.Intents.default())
        self.config = config
        # Alarms already sent today, keyed by (time, message). Deliberately keyed
        # by content rather than list position so that editing config.yaml doesn't
        # renumber alarms and make an already-sent one fire twice. Memory only,
        # cleared at local midnight — a restart deliberately does not backfill.
        self._fired = set()
        self._fired_date = None
        self._config_sig = config_signature(CONFIG_PATH)

    async def setup_hook(self):
        self.tick.start()

    async def on_ready(self):
        log.info("connected as %s", self.user)

    @tasks.loop(seconds=TICK_SECONDS)
    async def tick(self):
        # Anything escaping this method would stop the loop permanently: the bot
        # would stay connected and silently never alarm again. Always keep ticking.
        try:
            await self.check_alarms()
        except Exception:  # noqa: BLE001
            log.exception("tick failed, continuing")

    def maybe_reload(self):
        """Re-read config.yaml if it changed on disk. Never raises.

        Unlike startup, a bad config here is NOT fatal: the bot keeps running on
        the last good one. Exiting would mean a typo saved mid-edit silently kills
        every future alarm, which is far worse than ignoring the edit.
        """
        signature = config_signature(CONFIG_PATH)
        if signature is None:
            log.error("config.yaml is missing, keeping the config already loaded")
            return
        if signature == self._config_sig:
            return

        # Record the new signature before parsing, so a file left in a broken
        # state doesn't re-log the same error on every tick.
        self._config_sig = signature
        try:
            config = load_config(CONFIG_PATH)
        except ConfigError as exc:
            log.error("config.yaml changed but is invalid, keeping previous config: %s", exc)
            return

        self.config = config
        log.info(
            "reloaded config.yaml: %d alarm(s), timezone %s",
            len(config["alarms"]), config["tz"].key,
        )

    async def check_alarms(self):
        self.maybe_reload()

        # Read the wall clock fresh every tick rather than sleeping until the
        # next occurrence; that is what keeps this correct across DST shifts.
        now = datetime.now(self.config["tz"])
        today = now.date()
        if today != self._fired_date:
            self._fired = set()
            self._fired_date = today

        current = now.strftime("%H:%M")
        weekday = DAY_NAMES[now.weekday()]

        for alarm in self.config["alarms"]:
            if alarm["time"] != current or weekday not in alarm["days"]:
                continue
            key = (alarm["time"], alarm["message"])
            if key in self._fired:
                continue
            self._fired.add(key)
            await self.send_alarm(alarm)

    async def send_alarm(self, alarm):
        """Deliver one alarm. Never raises — a failed send must not kill the loop."""
        try:
            user = await self.fetch_user(self.config["user_id"])
            await user.send(alarm["message"])
            log.info("sent alarm %s at %s", alarm["index"], alarm["time"])
        except discord.Forbidden:
            log.error(
                "cannot DM user %s — they must share a server with the bot and allow "
                "DMs from server members",
                self.config["user_id"],
            )
        except discord.HTTPException as exc:
            log.error("Discord rejected alarm %s: %s", alarm["index"], exc)

    @tick.before_loop
    async def before_tick(self):
        # fetch_user needs a live connection, so don't tick before login.
        await self.wait_until_ready()

    async def on_error(self, event, *args, **kwargs):
        # Covers gateway event handlers only — tasks.loop exceptions do NOT route
        # here. The try/except in tick() is what keeps the schedule alive; don't
        # remove it on the assumption that this method catches those.
        log.exception("error in %s", event)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token.")

    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(f"config.yaml: {exc}")

    log.info(
        "loaded %d alarm(s), timezone %s",
        len(config["alarms"]), config["tz"].key,
    )
    try:
        AlarmBot(config).run(token, log_handler=None)
    except discord.LoginFailure:
        sys.exit(
            "Discord rejected the bot token in .env. Get a fresh one at "
            "https://discord.com/developers/applications -> your app -> Bot -> Reset Token."
        )


if __name__ == "__main__":
    main()
