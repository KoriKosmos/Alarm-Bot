"""Smoke test: can the bot actually DM the user in config.yaml?

Run this once before relying on the scheduler. Discord refuses DMs to users who
don't share a server with the bot or who block DMs from server members, and that
failure has nothing to do with your alarm times.

    python check_dm.py
"""

import asyncio
import logging
import os
import sys

import discord
from dotenv import load_dotenv

from bot import ConfigError, load_config


async def main():
    load_dotenv()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token.")

    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(f"config.yaml: {exc}")

    client = discord.Client(intents=discord.Intents.default())
    failed = []
    try:
        # login must stay inside the try, or a bad token skips the close() below
        # and buries the real error under "Unclosed client session" warnings.
        await client.login(token)
    except discord.LoginFailure:
        await client.close()
        sys.exit(
            "Discord rejected the bot token in .env.\n"
            "Get a fresh one at https://discord.com/developers/applications "
            "-> your app -> Bot -> Reset Token."
        )

    try:
        # Every configured user is tested, and one failure doesn't hide the rest —
        # knowing that two of three people are reachable is the useful answer.
        for name, user_id in config["users"].items():
            try:
                user = await client.fetch_user(user_id)
                await user.send("Alarm bot test message — DMs are working.")
                print(f"OK:   {name} ({user}) — test DM sent.")
            except discord.NotFound:
                failed.append(name)
                print(f"FAIL: {name} — no Discord user with ID {user_id}.")
            except discord.Forbidden:
                failed.append(name)
                print(
                    f"FAIL: {name} ({user_id}) — Discord refused the DM. They must share "
                    "a server with the bot and enable Settings -> Content & Social -> "
                    "allow DMs from server members."
                )
            except discord.HTTPException as exc:
                failed.append(name)
                print(f"FAIL: {name} ({user_id}) — Discord error: {exc}")
    finally:
        await client.close()

    if failed:
        sys.exit(f"\n{len(failed)} of {len(config['users'])} user(s) unreachable: "
                 f"{', '.join(failed)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
