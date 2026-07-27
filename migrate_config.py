"""Rewrite an old single-user config.yaml into the multi-user format.

Old:                          New:
    user_id: 123                  users:
    alarms:                         me: 123
      - time: "09:00"           alarms:
        days: weekdays            - time: "09:00"
        message: "hi"               days: weekdays
                                    message: "hi"
                                    to: [me]

Safe to run more than once — an already-migrated file is left alone. The bot
still reads the old format, so migrating is not urgent and never has to be done
under time pressure.

    python migrate_config.py                 # migrate config.yaml in place
    python migrate_config.py --dry-run       # print the result, change nothing
    python migrate_config.py --name kori     # call the user something else

Inside Docker the compose mount is read-only, so run it against the host copy:

    docker run --rm -v "$PWD:/work" -w /work --entrypoint python alarm-bot-bot \\
        migrate_config.py
"""

import argparse
import os
import shutil
import sys

import yaml

from bot import CONFIG_PATH, ConfigError, load_config

HEADER = """\
# Who to DM and in what timezone the alarm times below are interpreted.
# timezone: any IANA name, e.g. America/New_York, Europe/London, Asia/Kolkata.
# users: a name for each person, mapped to their Discord user ID. Enable
# Developer Mode in Discord, right-click a user -> Copy User ID.

# Each alarm needs a time (24-hour HH:MM), the days it runs, a message, and
# 'to' — which users get it. days accepts: everyday / daily, weekdays,
# weekends, or a list of mon tue wed thu fri sat sun.
# The running bot re-reads this file when you save it — no restart needed.
"""


class QuotedStr(str):
    """A string that always round-trips through YAML with quotes."""


def _represent_quoted(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'")


yaml.add_representer(QuotedStr, _represent_quoted, Dumper=yaml.SafeDumper)


def build_new_config(data, name):
    """Return the migrated dict, or None if `data` needs no migration."""
    if "users" in data:
        return None
    if "user_id" not in data:
        raise ConfigError(
            "this file has neither 'user_id' nor 'users', so it is not a config "
            "this script knows how to migrate"
        )

    alarms = data.get("alarms")
    if not isinstance(alarms, list) or not alarms:
        raise ConfigError("'alarms' must be a non-empty list")

    new_alarms = []
    for index, alarm in enumerate(alarms):
        if not isinstance(alarm, dict):
            raise ConfigError(f"alarms[{index}]: each alarm must be a mapping")
        # Rebuilt key by key so 'to' lands last and any unknown keys survive.
        migrated = {k: v for k, v in alarm.items() if k != "to"}
        # Always quote the time. Unquoted, YAML reads 21:30 as the number 1290,
        # so writing it bare would model exactly the mistake we want to prevent.
        if "time" in migrated:
            migrated["time"] = QuotedStr(migrated["time"])
        migrated["to"] = [name]
        new_alarms.append(migrated)

    return {
        "timezone": data.get("timezone"),
        "users": {name: data["user_id"]},
        "alarms": new_alarms,
    }


def dump(config):
    body = yaml.safe_dump(config, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return HEADER + "\n" + body


def readonly_hint(exc):
    """Extra guidance when the failure looks like the read-only compose mount."""
    if not isinstance(exc, PermissionError) and getattr(exc, "errno", None) != 30:
        return ""
    return (
        "\nThe compose file mounts the config read-only, so this cannot be run with "
        "`docker compose exec`. From the directory holding config.yaml:\n"
        '  docker run --rm -v "$PWD:/work" -w /work --entrypoint python '
        "alarm-bot-bot migrate_config.py"
    )


def backup_path(path):
    """First free <path>.bak, .bak.1, ... so an earlier backup is never clobbered."""
    candidate = path + ".bak"
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{path}.bak.{counter}"
        counter += 1
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", default=CONFIG_PATH,
                        help=f"config file to migrate (default: {CONFIG_PATH})")
    parser.add_argument("--name", default="me",
                        help="name to give the existing user (default: me)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the migrated file instead of writing it")
    args = parser.parse_args()

    if not args.name.strip():
        sys.exit("--name cannot be empty")
    name = args.name.strip()

    try:
        with open(args.path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError:
        sys.exit(f"no config file at {args.path}")
    except PermissionError:
        sys.exit(f"cannot read {args.path}: permission denied")
    except yaml.YAMLError as exc:
        sys.exit(f"{args.path} is not valid YAML, so there is nothing safe to migrate:\n{exc}")

    if not isinstance(data, dict):
        sys.exit(f"{args.path} must contain a top-level mapping")

    try:
        new_config = build_new_config(data, name)
    except ConfigError as exc:
        sys.exit(f"cannot migrate {args.path}: {exc}")

    if new_config is None:
        print(f"{args.path} already uses the 'users:' format — nothing to do.")
        return

    # Only ever transform a config the bot already accepts. Migrating a broken
    # file would turn one clear error into a confusing one after the rewrite.
    try:
        load_config(args.path)
    except ConfigError as exc:
        sys.exit(
            f"{args.path} is not valid as it stands, so it has been left alone.\n"
            f"  {exc}\nFix that first, then run this again."
        )

    text = dump(new_config)

    if args.dry_run:
        print(f"--- {args.path} would become ---\n")
        print(text, end="")
        return

    backup = backup_path(args.path)
    try:
        shutil.copy2(args.path, backup)
    except OSError as exc:
        sys.exit(f"could not back up {args.path} to {backup}: {exc}{readonly_hint(exc)}")

    # Write to a temp file in the same directory and rename, so an interrupted
    # write can never leave a half-written config behind.
    temp = args.path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, args.path)
    except OSError as exc:
        if os.path.exists(temp):
            os.unlink(temp)
        sys.exit(f"could not write {args.path}: {exc}{readonly_hint(exc)}"
                 f"\nYour original is at {backup}")

    # Prove the result actually loads before declaring success. If it doesn't,
    # put the original back rather than leaving a broken config in place.
    try:
        loaded = load_config(args.path)
    except ConfigError as exc:
        shutil.copy2(backup, args.path)
        sys.exit(
            f"migration produced a config the bot rejects, so it has been rolled back:\n"
            f"  {exc}\nYour original is unchanged (a copy is also at {backup})"
        )

    print(f"Migrated {args.path} (original saved as {backup}).")
    print(f"  users: {', '.join(loaded['users'])}")
    print(f"  {len(loaded['alarms'])} alarm(s), all addressed to '{name}'")
    print("The running bot picks this up on its next tick — no restart needed.")


if __name__ == "__main__":
    main()
