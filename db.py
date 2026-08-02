"""
MongoDB access layer for Premium Emoji Bot.
Uses Motor (async MongoDB driver) so it plays nicely with python-telegram-bot's
async handlers.
"""

import os
from datetime import datetime, timezone

import logging

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import PyMongoError

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "premium_emoji_bot")

logger = logging.getLogger("premium_emoji_bot.db")

# serverSelectionTimeoutMS kept short (5s) so a down/misconfigured DB fails
# fast instead of hanging every handler for 30s (pymongo's default).
client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB_NAME]

users_col = db["users"]
emoji_stats_col = db["emoji_stats"]
catalog_col = db["emoji_catalog"]


async def ensure_user(user_id: int, username: str | None, first_name: str | None) -> bool:
    """
    Upsert a user record, tracking first-seen and last-seen timestamps.
    Returns True on success, False if MongoDB is unreachable — callers should
    not let a DB outage block the actual Telegram reply.
    """
    now = datetime.now(timezone.utc)
    try:
        await users_col.update_one(
            {"_id": user_id},
            {
                "$set": {
                    "username": username,
                    "first_name": first_name,
                    "last_seen": now,
                },
                "$setOnInsert": {"first_seen": now},
            },
            upsert=True,
        )
        return True
    except PyMongoError:
        logger.exception("ensure_user failed — is MongoDB reachable at %s ?", MONGO_URI)
        return False


async def log_emoji_reply(user_id: int, chat_id: int, emoji_id: str) -> bool:
    """Log every time the bot replies with a custom emoji, for basic stats."""
    try:
        await emoji_stats_col.insert_one(
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "emoji_id": emoji_id,
                "ts": datetime.now(timezone.utc),
            }
        )
        return True
    except PyMongoError:
        logger.exception("log_emoji_reply failed — is MongoDB reachable at %s ?", MONGO_URI)
        return False


async def get_user_count() -> int | None:
    try:
        return await users_col.count_documents({})
    except PyMongoError:
        logger.exception("get_user_count failed — is MongoDB reachable at %s ?", MONGO_URI)
        return None


# ------------------------------------------------------------------
# Emoji catalog — bulk-imported packs + crowdsourced submissions
# ------------------------------------------------------------------

async def upsert_catalog_emoji(
    custom_emoji_id: str,
    fallback: str,
    source: str,
    added_by: int | None = None,
) -> bool:
    """
    Add or update one catalog entry. `source` is either a sticker-pack short
    name (bulk import) or "user" (crowdsourced). Uses custom_emoji_id as the
    document _id so re-imports / re-submissions just refresh last_seen.
    """
    now = datetime.now(timezone.utc)
    try:
        await catalog_col.update_one(
            {"_id": custom_emoji_id},
            {
                "$set": {
                    "fallback": fallback,
                    "source": source,
                    "last_seen": now,
                },
                "$setOnInsert": {
                    "added_by": added_by,
                    "added_at": now,
                    "usage_count": 0,
                },
            },
            upsert=True,
        )
        return True
    except PyMongoError:
        logger.exception("upsert_catalog_emoji failed — is MongoDB reachable at %s ?", MONGO_URI)
        return False


async def bulk_upsert_catalog(entries: list[tuple[str, str]], source: str) -> int | None:
    """
    Bulk-import entries as [(custom_emoji_id, fallback), ...] from a known
    pack. Returns the number of entries written, or None if MongoDB is
    unreachable (distinct from 0, which means genuinely nothing to write).
    """
    if not entries:
        return 0
    now = datetime.now(timezone.utc)
    try:
        from pymongo import UpdateOne

        ops = [
            UpdateOne(
                {"_id": eid},
                {
                    "$set": {"fallback": fallback, "source": source, "last_seen": now},
                    "$setOnInsert": {"added_by": None, "added_at": now, "usage_count": 0},
                },
                upsert=True,
            )
            for eid, fallback in entries
        ]
        result = await catalog_col.bulk_write(ops, ordered=False)
        return result.upserted_count + result.modified_count
    except PyMongoError:
        logger.exception("bulk_upsert_catalog failed — is MongoDB reachable at %s ?", MONGO_URI)
        return None


async def get_catalog_page(skip: int, limit: int) -> list[dict]:
    """Return a page of catalog entries, newest first."""
    try:
        cursor = catalog_col.find({}).sort("added_at", -1).skip(skip).limit(limit)
        return [doc async for doc in cursor]
    except PyMongoError:
        logger.exception("get_catalog_page failed — is MongoDB reachable at %s ?", MONGO_URI)
        return []


async def get_catalog_count() -> int:
    try:
        return await catalog_col.count_documents({})
    except PyMongoError:
        logger.exception("get_catalog_count failed — is MongoDB reachable at %s ?", MONGO_URI)
        return 0


async def get_catalog_entry(custom_emoji_id: str) -> dict | None:
    try:
        return await catalog_col.find_one({"_id": custom_emoji_id})
    except PyMongoError:
        logger.exception("get_catalog_entry failed — is MongoDB reachable at %s ?", MONGO_URI)
        return None


async def increment_usage(custom_emoji_id: str) -> None:
    try:
        await catalog_col.update_one({"_id": custom_emoji_id}, {"$inc": {"usage_count": 1}})
    except PyMongoError:
        logger.exception("increment_usage failed — is MongoDB reachable at %s ?", MONGO_URI)


async def update_catalog_entry(custom_emoji_id: str, fallback: str) -> bool | None:
    """
    Admin edit: change the stored fallback text for an existing catalog
    entry. Returns True if a document was matched & updated, False if no
    entry with that ID exists, None if MongoDB is unreachable.
    """
    try:
        result = await catalog_col.update_one(
            {"_id": custom_emoji_id}, {"$set": {"fallback": fallback}}
        )
        return result.matched_count > 0
    except PyMongoError:
        logger.exception("update_catalog_entry failed — is MongoDB reachable at %s ?", MONGO_URI)
        return None


async def delete_catalog_entry(custom_emoji_id: str) -> bool | None:
    """
    Admin delete: remove an entry from the catalog entirely. Returns True if
    something was deleted, False if no entry with that ID existed, None if
    MongoDB is unreachable.
    """
    try:
        result = await catalog_col.delete_one({"_id": custom_emoji_id})
        return result.deleted_count > 0
    except PyMongoError:
        logger.exception("delete_catalog_entry failed — is MongoDB reachable at %s ?", MONGO_URI)
        return None
