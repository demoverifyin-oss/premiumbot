"""
Premium Emoji Bot
==================
- /start welcome message with a custom (premium) emoji + inline buttons.
- Auto-detects when someone sends a message containing a custom emoji and
  replies back with the custom_emoji_id(s) it found, plus one of the bot's
  own premium emojis.
- Stores users in MongoDB (Motor / async).
- /stats, /addpack, /editentry, /delentry are admin-only (ADMIN_IDS in .env).
- UI icons (🖼 catalog, 👥 users, ⬅️➡️ nav, etc.) are rendered as the bot's
  own premium custom emoji wherever we have an ID for them, and fall back
  to DEFAULT_EMOJI_ID for anything else. Gallery numbers stay plain text
  and are laid out as a CATALOG_COLUMNS-wide grid.
- Sending the bot a message with a custom emoji gets you its ID plus a
  tappable "see code" button per emoji.

Run:
    pip install -r requirements.txt
    cp .env.example .env   # then fill in BOT_TOKEN, MONGO_URI, DEFAULT_EMOJI_ID, ADMIN_IDS
    python main.py
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    Update,
)
from telegram.constants import MessageEntityType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Resolve .env relative to this file's own folder, not the current working
# directory — so it loads correctly no matter where you run the script from.
# This MUST happen before `import db`, because db.py reads MONGO_URI from the
# environment at import time — importing db first would make it fall back to
# its localhost default even when .env has the real (Atlas) value.
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)

import db

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_EMOJI_ID = os.getenv("DEFAULT_EMOJI_ID", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

BOT_USERNAME = ""  # filled in at startup via get_me()
CATALOG_PAGE_SIZE = 8
CATALOG_COLUMNS = 5  # how many entries per row in the catalog text grid
BUTTONS_PER_ROW = 3  # was 4 — 3 per row leaves more breathing room, more rows

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("premium_emoji_bot")


# ------------------------------------------------------------------
# Fixed premium-emoji IDs for the bot's own UI (not user-submitted ones)
# ------------------------------------------------------------------

# Specific icon -> custom_emoji_id overrides. Any emoji used in bot text
# that ISN'T in this map still gets rendered as a premium emoji, just using
# DEFAULT_EMOJI_ID instead (see icon_entity()). NOTE: "➡️"/"⬅️" here only take
# effect if those arrows ever appear inside a message body — the Prev/Next
# *buttons* are plain Bot-API button labels and can't carry custom-emoji
# entities at all, so they'll stay as normal unicode arrows regardless.
EMOJI_ICON_IDS = {
    "🖼": "6255693594032605539",   # catalog icon
    "👥": "5150276257375586099",   # users icon
    "➡️": "5951665890079544884",   # next arrow
    "⬅️": "5951665890079544884",   # prev arrow
}


def _mask_mongo_uri(uri: str) -> str:
    """Hide credentials when logging the Mongo URI, keep the host visible."""
    if "@" in uri:
        scheme_and_creds, host_part = uri.rsplit("@", 1)
        scheme = scheme_and_creds.split("://")[0]
        return f"{scheme}://***:***@{host_part}"
    return uri


logger.info(
    "Loaded .env from %s | BOT_TOKEN present: %s | DEFAULT_EMOJI_ID present: %s | ADMIN_IDS: %s | MONGO_URI: %s",
    ENV_PATH,
    bool(BOT_TOKEN),
    bool(DEFAULT_EMOJI_ID),
    sorted(ADMIN_IDS) or "none set",
    _mask_mongo_uri(os.getenv("MONGO_URI", "not set")),
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def utf16_len(s: str) -> int:
    """Telegram entity offsets/lengths are counted in UTF-16 code units,
    not Python codepoints — emoji and other astral characters take 2 units."""
    return len(s.encode("utf-16-le")) // 2


def build_custom_emoji_entity(emoji_id: str, offset: int = 0, length: int = 1) -> MessageEntity:
    """Build a single custom-emoji MessageEntity."""
    return MessageEntity(
        type=MessageEntityType.CUSTOM_EMOJI,
        offset=offset,
        length=length,
        custom_emoji_id=emoji_id,
    )


def icon_entity(icon: str, offset: int = 0) -> MessageEntity | None:
    """
    Entity for a UI icon character sitting at `offset` in some text. Uses a
    specific mapped custom_emoji_id if we have one (EMOJI_ICON_IDS), else
    falls back to DEFAULT_EMOJI_ID so it still renders as a premium emoji.
    Returns None if neither is available (leaves the plain unicode emoji as-is).
    """
    emoji_id = EMOJI_ICON_IDS.get(icon) or DEFAULT_EMOJI_ID
    if not emoji_id:
        return None
    return build_custom_emoji_entity(emoji_id, offset=offset, length=utf16_len(icon))


def icon_message(icon: str, rest: str) -> tuple[str, list[MessageEntity] | None]:
    """Build `"{icon} {rest}"` plus the matching entity list in one go — used
    for every short status reply (admin-only, warnings, confirmations, ...)
    so their leading icon is always a premium emoji too."""
    text = f"{icon} {rest}"
    entity = icon_entity(icon, offset=0)
    return text, ([entity] if entity else None)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu_keyboard(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    """Minimalistic inline keyboard for the main menu. Stats button is only
    shown to admins — regular users don't get a button that just tells them
    'admin-only' when they tap it. (Button labels can't carry premium-emoji
    entities — that's a Bot API limitation — so these stay plain unicode.)"""
    top_row = [InlineKeyboardButton("🖼 Gallery", callback_data="gallery:0")]
    if is_admin_user:
        top_row.append(InlineKeyboardButton("📊 Stats", callback_data="stats"))

    rows = [
        top_row,
        [
            InlineKeyboardButton("💬 Support", url="https://t.me/your_support_handle"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def build_code_snippet(emoji_id: str, fallback: str) -> str:
    """Ready-to-paste Python + HTML snippets for a given custom emoji ID."""
    return (
        "Python (python-telegram-bot):\n"
        "<pre>"
        "entities=[MessageEntity(\n"
        "    type=\"custom_emoji\", offset=0, length=1,\n"
        f"    custom_emoji_id=\"{emoji_id}\",\n"
        ")]\n"
        f"text = \"{fallback}\""
        "</pre>\n\n"
        "HTML (Bot API sendMessage, parse_mode=HTML):\n"
        "<pre>"
        f"&lt;tg-emoji emoji-id=\"{emoji_id}\"&gt;{fallback}&lt;/tg-emoji&gt;"
        "</pre>"
    )


def build_deep_link(emoji_id: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=emoji_{emoji_id}"


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ok = await db.ensure_user(user.id, user.username, user.first_name)
    if not ok:
        logger.warning("DB write failed for user %s — continuing without it.", user.id)

    # Deep-link: t.me/YourBot?start=emoji_<id> jumps straight to that emoji's code snippet
    if context.args and context.args[0].startswith("emoji_"):
        emoji_id = context.args[0][len("emoji_"):]
        await send_emoji_snippet(update.message, emoji_id)
        return

    icon = "⭐"
    text = (
        f"{icon} Welcome to Premium Emoji Bot\n\n"
        "Send me any message containing a premium (custom) emoji and I'll "
        "reply with its ID — and show off one of my own.\n\n"
        "Browse the catalog with /gallery, or use /help to see all commands."
    )
    entity = icon_entity(icon, offset=0)
    entities = [entity] if entity else None

    await update.message.reply_text(
        text=text,
        entities=entities,
        reply_markup=main_menu_keyboard(is_admin(user.id)),
    )


async def send_emoji_snippet(message, emoji_id: str):
    """Shared by /start deep-link and the gallery 'Get' button."""
    entry = await db.get_catalog_entry(emoji_id)
    fallback = entry["fallback"] if entry else "⭐"

    header = "Here's that emoji: "
    full_text = header + fallback
    entities = [build_custom_emoji_entity(emoji_id, offset=utf16_len(header), length=utf16_len(fallback))]

    await message.reply_text(text=full_text, entities=entities)
    await message.reply_text(
        text=f"{build_code_snippet(emoji_id, fallback)}\n\nShare link:\n{build_deep_link(emoji_id)}",
        parse_mode="HTML",
    )
    await db.increment_usage(emoji_id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    icon = "📋"
    lines = [
        f"{icon} Available Commands\n",
        "/start — Open the main menu",
        "/help — Show this message",
        "/gallery — Browse the emoji catalog",
    ]
    if is_admin(user.id):
        lines.append("/stats — Bot usage stats (admin-only)")
        lines.append("/addpack <short_name> — Bulk-import a sticker pack (admin-only)")
        lines.append("/editentry <emoji_id> <new_fallback> — Edit a catalog entry (admin-only)")
        lines.append("/delentry <emoji_id> — Delete a catalog entry (admin-only)")
    lines.append("\nSend any message containing a premium emoji and I'll add it to the catalog and reply with its custom_emoji_id.")

    text = "\n".join(lines)
    entity = icon_entity(icon, offset=0)
    entities = [entity] if entity else None
    await update.message.reply_text(text, entities=entities)


async def build_stats_text() -> tuple[str, list[MessageEntity]]:
    """Shared by /stats and the gallery 'Stats' button."""
    count = await db.get_user_count()
    catalog_count = await db.get_catalog_count()

    if count is None:
        icon = "⚠️"
        text = f"{icon} Stats unavailable right now — database unreachable."
        entity = icon_entity(icon, offset=0)
        return text, ([entity] if entity else [])

    users_icon = "👥"
    catalog_icon = "🖼"
    line1 = f"{users_icon} Total users seen: {count}\n"
    line2 = f"{catalog_icon} Catalog size: {catalog_count}"
    text = line1 + line2

    entities = []
    e1 = icon_entity(users_icon, offset=0)
    if e1:
        entities.append(e1)
    e2 = icon_entity(catalog_icon, offset=utf16_len(line1))
    if e2:
        entities.append(e2)
    return text, entities


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        text, entities = icon_message("⛔", "This command is admin-only.")
        await update.message.reply_text(text, entities=entities)
        return

    text, entities = await build_stats_text()
    await update.message.reply_text(text, entities=entities or None)


async def addpack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: bulk-import a public custom-emoji sticker pack by its short name."""
    user = update.effective_user
    if not is_admin(user.id):
        text, entities = icon_message("⛔", "This command is admin-only.")
        await update.message.reply_text(text, entities=entities)
        return

    if not context.args:
        await update.message.reply_text("Usage: /addpack <sticker_pack_short_name>")
        return

    pack_name = context.args[0]
    try:
        sticker_set = await context.bot.get_sticker_set(pack_name)
    except Exception as e:  # Telegram raises BadRequest for unknown/invalid packs
        text, entities = icon_message("⚠️", f"Couldn't fetch pack '{pack_name}': {e}")
        await update.message.reply_text(text, entities=entities)
        return

    entries = [
        (sticker.custom_emoji_id, sticker.emoji or "⭐")
        for sticker in sticker_set.stickers
        if sticker.custom_emoji_id
    ]

    if not entries:
        text, entities = icon_message("⚠️", f"'{pack_name}' has no custom emoji stickers.")
        await update.message.reply_text(text, entities=entities)
        return

    written = await db.bulk_upsert_catalog(entries, source=pack_name)
    if written is None:
        text, entities = icon_message(
            "⚠️", f"Found {len(entries)} emoji in '{pack_name}' but couldn't save them — database unreachable."
        )
        await update.message.reply_text(text, entities=entities)
    else:
        text, entities = icon_message("✅", f"Imported {written}/{len(entries)} emoji from '{pack_name}'.")
        await update.message.reply_text(text, entities=entities)


async def editentry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: change the fallback text stored for a catalog entry."""
    user = update.effective_user
    if not is_admin(user.id):
        text, entities = icon_message("⛔", "This command is admin-only.")
        await update.message.reply_text(text, entities=entities)
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /editentry <custom_emoji_id> <new_fallback_char>")
        return

    emoji_id = context.args[0]
    new_fallback = context.args[1]

    entry = await db.get_catalog_entry(emoji_id)
    if not entry:
        text, entities = icon_message("⚠️", f"No catalog entry found for ID {emoji_id}.")
        await update.message.reply_text(text, entities=entities)
        return

    ok = await db.update_catalog_entry(emoji_id, new_fallback)
    if ok is None:
        text, entities = icon_message("⚠️", "Couldn't save — database unreachable.")
        await update.message.reply_text(text, entities=entities)
        return
    if not ok:
        text, entities = icon_message("⚠️", f"No catalog entry found for ID {emoji_id}.")
        await update.message.reply_text(text, entities=entities)
        return

    icon = "✅"
    header = f"{icon} Updated. Now: "
    text = header + new_fallback
    entities = []
    icon_ent = icon_entity(icon, offset=0)
    if icon_ent:
        entities.append(icon_ent)
    entities.append(build_custom_emoji_entity(emoji_id, offset=utf16_len(header), length=utf16_len(new_fallback)))
    await update.message.reply_text(text, entities=entities)


async def delentry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: remove a catalog entry entirely."""
    user = update.effective_user
    if not is_admin(user.id):
        text, entities = icon_message("⛔", "This command is admin-only.")
        await update.message.reply_text(text, entities=entities)
        return

    if not context.args:
        await update.message.reply_text("Usage: /delentry <custom_emoji_id>")
        return

    emoji_id = context.args[0]
    ok = await db.delete_catalog_entry(emoji_id)
    if ok is None:
        text, entities = icon_message("⚠️", "Couldn't delete — database unreachable.")
        await update.message.reply_text(text, entities=entities)
    elif ok:
        text, entities = icon_message("🗑", f"Deleted catalog entry {emoji_id}.")
        await update.message.reply_text(text, entities=entities)
    else:
        text, entities = icon_message("⚠️", f"No catalog entry found for ID {emoji_id}.")
        await update.message.reply_text(text, entities=entities)


async def gallery_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_gallery_page(update.message, page=0)


async def send_gallery_page(message, page: int, edit: bool = False):
    total = await db.get_catalog_count()
    if total == 0:
        icon = "🖼"
        text = f"{icon} Catalog is empty so far — send me a premium emoji to add one, or ask an admin to /addpack a set."
        entity = icon_entity(icon, offset=0)
        entities = [entity] if entity else None
        if edit:
            await message.edit_text(text, entities=entities)
        else:
            await message.reply_text(text, entities=entities)
        return

    skip = page * CATALOG_PAGE_SIZE
    entries = await db.get_catalog_page(skip=skip, limit=CATALOG_PAGE_SIZE)

    if not entries:
        # Page is past the end (e.g. catalog shrank) — bounce back to page 0
        # instead of showing an empty page.
        if page != 0:
            await send_gallery_page(message, page=0, edit=edit)
        return

    header_icon = "🖼"
    header = f"{header_icon} Emoji Catalog (page {page + 1}) — tap a number to get its code:\n\n"
    body = ""
    entities = []

    header_entity = icon_entity(header_icon, offset=0)
    if header_entity:
        entities.append(header_entity)

    cursor = utf16_len(header)

    # Numbers keep counting up across pages (skip+1, skip+2, ...) instead of
    # resetting to 1 every page. Laid out as a CATALOG_COLUMNS-wide grid
    # (plain numbers — no digit emoji, they read cleaner this way) instead
    # of one entry per line.
    for idx, entry in enumerate(entries):
        global_num = skip + idx + 1
        fallback = entry.get("fallback", "⭐")

        prefix = f"{global_num}."
        body += prefix
        cursor += utf16_len(prefix)

        entities.append(build_custom_emoji_entity(entry["_id"], offset=cursor, length=utf16_len(fallback)))
        body += fallback
        cursor += utf16_len(fallback)

        is_last_entry = idx == len(entries) - 1
        is_row_end = (idx + 1) % CATALOG_COLUMNS == 0
        sep = "\n" if (is_last_entry or is_row_end) else "  "
        body += sep
        cursor += utf16_len(sep)

    full_text = header + body

    # Plain-number button labels — the Bot API doesn't support custom-emoji
    # entities inside inline keyboard button text, only in message bodies.
    number_buttons = [
        InlineKeyboardButton(str(skip + i + 1), callback_data=f"get:{entry['_id']}")
        for i, entry in enumerate(entries)
    ]
    button_rows = [number_buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(number_buttons), BUTTONS_PER_ROW)]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"gallery:{page - 1}"))
    if skip + CATALOG_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"gallery:{page + 1}"))
    if nav_row:
        button_rows.append(nav_row)

    markup = InlineKeyboardMarkup(button_rows)

    if edit:
        await message.edit_text(text=full_text, entities=entities, reply_markup=markup)
    else:
        await message.reply_text(text=full_text, entities=entities, reply_markup=markup)


async def gallery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("gallery:"):
        page = int(data.split(":", 1)[1])
        await send_gallery_page(query.message, page=page, edit=True)
    elif data.startswith("get:"):
        emoji_id = data.split(":", 1)[1]
        await send_emoji_snippet(query.message, emoji_id)
    elif data == "stats":
        if not is_admin(query.from_user.id):
            text, entities = icon_message("⛔", "Stats are admin-only.")
            await query.message.reply_text(text, entities=entities)
            return
        text, entities = await build_stats_text()
        await query.message.reply_text(text, entities=entities or None)


async def handle_custom_emoji_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Triggered on any message that contains at least one custom_emoji entity.
    Replies with each incoming emoji rendered next to its ID, plus one of
    the bot's own premium emojis at the end. This is how regular (non-admin)
    users get to see a custom_emoji_id — just send the bot that emoji.
    """
    message = update.effective_message
    user = update.effective_user
    await db.ensure_user(user.id, user.username, user.first_name)

    # parse_entities returns {MessageEntity: original_fallback_text}, correctly
    # sliced from the message — this is what lets us re-render the same emoji.
    incoming = message.parse_entities(types=[MessageEntityType.CUSTOM_EMOJI])

    header_icon = "🔎"
    header = f"{header_icon} Custom emoji ID(s) in that message:\n"
    body = ""
    entities = []

    header_entity = icon_entity(header_icon, offset=0)
    if header_entity:
        entities.append(header_entity)

    cursor = utf16_len(header)

    if incoming:
        for entity, fallback in incoming.items():
            line = f"{fallback} → {entity.custom_emoji_id}\n"
            entities.append(
                build_custom_emoji_entity(entity.custom_emoji_id, offset=cursor, length=utf16_len(fallback))
            )
            body += line
            cursor += utf16_len(line)
    else:
        body = "none found\n"
        cursor += utf16_len(body)

    full_text = header + body

    # One button per detected emoji so the user can tap through to the
    # ready-to-paste Python/HTML snippet (same "get:" flow the gallery uses)
    # instead of us dumping every snippet inline.
    format_buttons = [
        InlineKeyboardButton(f"📋 {fallback} code", callback_data=f"get:{entity.custom_emoji_id}")
        for entity, fallback in incoming.items()
    ]
    button_rows = [format_buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(format_buttons), BUTTONS_PER_ROW)]
    markup = InlineKeyboardMarkup(button_rows) if button_rows else None

    if DEFAULT_EMOJI_ID:
        star = "⭐"
        footer = "\nHere's one of mine "
        full_text += footer + star
        entities.append(
            build_custom_emoji_entity(DEFAULT_EMOJI_ID, offset=cursor + utf16_len(footer), length=utf16_len(star))
        )
        await message.reply_text(text=full_text, entities=entities, reply_markup=markup)
        await db.log_emoji_reply(user.id, message.chat_id, DEFAULT_EMOJI_ID)
    else:
        await message.reply_text(text=full_text, entities=entities, reply_markup=markup)

    if incoming:
        for entity, fallback in incoming.items():
            await db.upsert_catalog_emoji(
                entity.custom_emoji_id, fallback, source="user", added_by=user.id
            )
        logger.info(
            "Custom emoji IDs received from user %s: %s",
            user.id,
            [e.custom_emoji_id for e in incoming],
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all so a failure in one handler never silently kills a reply."""
    logger.error("Unhandled exception while processing update:", exc_info=context.error)


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------

async def post_init(app: Application):
    global BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username
    logger.info("Bot username resolved: @%s", BOT_USERNAME)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Check that .env sits next to this script and has BOT_TOKEN=...")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty — /stats, /addpack, /editentry, /delentry will be unusable by anyone until you set it.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("addpack", addpack_command))
    app.add_handler(CommandHandler("editentry", editentry_command))
    app.add_handler(CommandHandler("delentry", delentry_command))
    app.add_handler(CommandHandler("gallery", gallery_command))
    app.add_handler(CallbackQueryHandler(gallery_callback))

    # Filters.Entity catches messages containing at least one custom_emoji entity
    app.add_handler(
        MessageHandler(
            filters.Entity(MessageEntityType.CUSTOM_EMOJI),
            handle_custom_emoji_message,
        )
    )

    app.add_error_handler(error_handler)

    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
