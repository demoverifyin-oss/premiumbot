"""
Standalone helper — NOT part of the running bot.
Pulls recent updates via getUpdates and prints any custom_emoji IDs seen,
so you can copy one into DEFAULT_EMOJI_ID in your .env.

Usage:
    python utils/scrape_emoji_ids.py
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise SystemExit("Set BOT_TOKEN in your .env first.")

url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
data = requests.get(url).json()

found = set()
for result in data.get("result", []):
    msg = result.get("message", {})
    for entity in msg.get("entities", []):
        if entity.get("type") == "custom_emoji":
            found.add(entity["custom_emoji_id"])

if found:
    print("Custom emoji IDs found:")
    for eid in found:
        print(f"  - {eid}")
else:
    print("No custom emoji entities found in recent updates.")
