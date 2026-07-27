"""Discord alarm bot: DMs one or more users at times listed in config.yaml."""

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
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
    if isinstance(raw, int) and not isinstance(raw, bool):
        # YAML 1.1 reads an unquoted 21:30 as sexagesimal — 21*60+30 = 1290 — so
        # the value arrives here as a number and the obvious error message would
        # be baffling. Name the real problem instead.
        hours, minutes = divmod(raw, 60)
        if 0 <= hours < 24:
            raise ConfigError(
                f'{where}: \'time\' must be quoted. YAML reads an unquoted 21:30 as '
                f'the number {raw}; write it as "{hours:02d}:{minutes:02d}"'
            )

    text = str(raw).strip()
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ConfigError(
            f"{where}: 'time' must be 24-hour HH:MM (e.g. 09:00 or 22:30), got {raw!r}"
        ) from None


def parse_user_id(raw, where):
    """Validate a single Discord user ID."""
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{where}: must be a Discord user ID (a long number), got {raw!r}"
        ) from None
    if user_id <= 0:
        raise ConfigError(f"{where}: is still the placeholder — set a real Discord user ID")
    return user_id


def parse_users(data):
    """Return ({name: user_id}, used_legacy_format).

    The old single-user `user_id:` format is still accepted. The server pulls new
    code on every restart, so breaking the schema outright would stop the alarms
    the moment it redeployed. migrate_config.py rewrites the file properly.
    """
    raw = data.get("users")
    if raw is None:
        if "user_id" not in data:
            raise ConfigError(
                "'users' must be a mapping of name -> Discord user ID, e.g.\n"
                "  users:\n"
                "    me: 123456789012345678"
            )
        log.warning(
            "config.yaml still uses the old 'user_id:' format. It works, but run "
            "migrate_config.py to move to 'users:' with per-alarm 'to:'"
        )
        return {"me": parse_user_id(data["user_id"], "'user_id'")}, True

    if not isinstance(raw, dict) or not raw:
        raise ConfigError("'users' must be a non-empty mapping of name -> Discord user ID")

    users = {}
    for name, value in raw.items():
        key = str(name).strip()
        if not key:
            raise ConfigError("'users' contains an entry with an empty name")
        users[key] = parse_user_id(value, f"users[{key!r}]")
    return users, False


def parse_recipients(raw, users, where):
    """Resolve an alarm's `to` into [(name, user_id), ...], keeping config order."""
    if raw is None:
        if len(users) == 1:
            return [next(iter(users.items()))]
        # Defaulting to everyone would mean adding a user silently signs them up
        # for every existing alarm. Make it explicit instead.
        raise ConfigError(
            f"{where}: 'to' is required once more than one user is defined. "
            f"Known users: {', '.join(sorted(users))}"
        )

    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where}: 'to' must be a user name or a non-empty list of names")

    recipients = []
    seen_ids = set()
    for entry in raw:
        name = str(entry).strip()
        if name not in users:
            raise ConfigError(
                f"{where}: unknown user {name!r}. "
                f"Known users: {', '.join(sorted(users)) or 'none'}"
            )
        # Deduped by resolved ID rather than name, so two aliases pointing at the
        # same person don't produce two DMs.
        if users[name] in seen_ids:
            continue
        seen_ids.add(users[name])
        recipients.append((name, users[name]))
    return recipients


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

    users, legacy = parse_users(data)

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
            "recipients": parse_recipients(entry.get("to"), users, where),
        })

    return {"tz": tz, "users": users, "alarms": alarms, "legacy": legacy}


class AlarmBot(discord.Client):
    def __init__(self, config):
        # No privileged intents: schedules come from the config file, so the
        # bot never reads message content and needs nothing toggled in the portal.
        super().__init__(intents=discord.Intents.default())
        self.config = config
        # Alarms already sent today, keyed by (time, message, user_id). Keyed by
        # content rather than list position so that editing config.yaml doesn't
        # renumber alarms and make an already-sent one fire twice, and per user so
        # that a migration or a rename can't re-send what someone already got.
        # Memory only,
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
            "reloaded config.yaml: %d alarm(s) for %d user(s), timezone %s",
            len(config["alarms"]), len(config["users"]), config["tz"].key,
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
            # Tracked per recipient, so one person's failed or blocked DM never
            # suppresses anyone else's.
            for name, user_id in alarm["recipients"]:
                key = (alarm["time"], alarm["message"], user_id)
                if key in self._fired:
                    continue
                self._fired.add(key)
                try:
                    await self.send_alarm(alarm, name, user_id)
                except Exception:  # noqa: BLE001
                    # send_alarm handles Discord's own errors; this catches the
                    # unexpected ones so that failing to reach one person can't
                    # cost everybody else on this alarm — or any later alarm in
                    # the same tick — their message.
                    log.exception("unexpected failure sending the %s alarm to %s",
                                  alarm["time"], name)

    async def send_alarm(self, alarm, name, user_id):
        """Deliver one alarm to one person.

        Never raises — a failed send must neither kill the loop nor stop the
        remaining recipients of the same alarm from getting theirs.
        """
        try:
            user = await self.fetch_user(user_id)
            await user.send(alarm["message"])
            log.info("sent the %s alarm to %s", alarm["time"], name)
        except discord.Forbidden:
            log.error(
                "cannot DM %s (%s) — they must share a server with the bot and allow "
                "DMs from server members",
                name, user_id,
            )
        except discord.HTTPException as exc:
            log.error("Discord rejected the %s alarm to %s: %s", alarm["time"], name, exc)

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
        "loaded %d alarm(s) for %d user(s) (%s), timezone %s",
        len(config["alarms"]), len(config["users"]),
        ", ".join(config["users"]), config["tz"].key,
    )
    try:
        AlarmBot(config).run(token, log_handler=None)
    except discord.LoginFailure:
        sys.exit(
            "Discord rejected the bot token in .env. Get a fresh one at "
            "https://discord.com/developers/applications -> your app -> Bot -> Reset Token."
        )
    except aiohttp.ClientError as exc:
        # Discord is unreachable. In a container this is usually just DNS not
        # being ready yet in the first second after boot. Exit non-zero with one
        # clean line and let the supervisor restart us — Docker's restart policy
        # already does this well, so don't reimplement backoff here. Once the
        # bot is connected, discord.py handles reconnects itself.
        sys.exit(f"Cannot reach Discord ({exc}). Exiting so the restart policy can retry.")


if __name__ == "__main__":
    main()
