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
    try:
        # login must stay inside the try, or a bad token skips the close() below
        # and buries the real error under "Unclosed client session" warnings.
        await client.login(token)
        user = await client.fetch_user(config["user_id"])
        await user.send("Alarm bot test message — DMs are working.")
        print(f"OK: sent a test DM to {user} ({user.id}).")
    except discord.LoginFailure:
        sys.exit(
            "Discord rejected the bot token in .env.\n"
            "Get a fresh one at https://discord.com/developers/applications "
            "-> your app -> Bot -> Reset Token."
        )
    except discord.NotFound:
        sys.exit(f"No Discord user with ID {config['user_id']}. Check user_id in config.yaml.")
    except discord.Forbidden:
        sys.exit(
            f"Discord refused the DM to {config['user_id']}.\n"
            "Invite the bot to a server you are in, and enable "
            "Settings -> Content & Social -> allow DMs from server members."
        )
    except discord.HTTPException as exc:
        sys.exit(f"Discord error: {exc}")
    finally:
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
