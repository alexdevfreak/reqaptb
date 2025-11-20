"""
bot.py - Advanced Telegram Channel/Group Auto-Approve Bot
Requires: python-telegram-bot v20+, python-dotenv

Environment variables (put into .env or your hosting env):
- BOT_TOKEN
- ADMIN_IDS (comma-separated Telegram user IDs)
- LOG_CHANNEL_ID (optional)
- DATA_CHANNEL_ID (the "database" channel where approval logs will be sent)
- PERSIST_FILE (optional, defaults to data.json)
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

from telegram import Update, Chat, ChatJoinRequest
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    filters,
)

# Load .env
load_dotenv()

BOT_TOKEN: Optional[str] = os.getenv("BOT_TOKEN")
ADMIN_IDS_RAW: str = os.getenv("ADMIN_IDS", "")
# Parse admin ids safely (ignore invalid entries)
ADMIN_IDS: List[int] = []
for part in ADMIN_IDS_RAW.split(","):
    part = part.strip()
    if not part:
        continue
    try:
        ADMIN_IDS.append(int(part))
    except ValueError:
        # ignore invalid id
        pass

def _int_env(name: str, default: int = 0) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default

LOG_CHANNEL_ID: int = _int_env("LOG_CHANNEL_ID", 0)
DATA_CHANNEL_ID: int = _int_env("DATA_CHANNEL_ID", 0)
PERSIST_FILE: str = os.getenv("PERSIST_FILE", "data.json")

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory data; persisted to disk
DATA: Dict[str, Any] = {
    "channels": {},  # chat_id_str -> {"title": str, "users": [user_id, ...]}
    "promotion": "",  # global promotion message (admin-set)
}


def load_data() -> None:
    global DATA
    if os.path.exists(PERSIST_FILE):
        try:
            with open(PERSIST_FILE, "r", encoding="utf-8") as f:
                DATA = json.load(f)
                logger.info("Loaded data from %s", PERSIST_FILE)
        except Exception as e:
            logger.warning("Could not load data file %s: %s", PERSIST_FILE, e)


def save_data() -> None:
    try:
        with open(PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False, indent=2)
            logger.info("Saved data to %s", PERSIST_FILE)
    except Exception as e:
        logger.exception("Failed to save data: %s", e)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Hi — I am your Auto-Approve Bot.\n\n"
        "Admin Commands:\n"
        "/users - Show stored users count\n"
        "/broadcast <message> - Send message to all stored users\n"
        "/promotion <text> - Set promotion message sent after approval\n"
        "/details - Show join counts per channel\n"
    )
    await update.effective_chat.send_message(text)


async def set_promotion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_chat.send_message("You are not authorized to use this command.")
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.effective_chat.send_message("Usage: /promotion Your promotional message here")
        return

    DATA["promotion"] = text
    save_data()
    await update.effective_chat.send_message("Promotion message saved.")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_chat.send_message("You are not authorized to use this command.")
        return

    total = 0
    for info in DATA.get("channels", {}).values():
        total += len(info.get("users", []))
    await update.effective_chat.send_message(f"Total stored users: {total}")


async def details_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_chat.send_message("You are not authorized to use this command.")
        return

    lines: List[str] = []
    for cid, info in DATA.get("channels", {}).items():
        title = info.get("title") or str(cid)
        count = len(info.get("users", []))
        lines.append(f"{title} ({cid}) - {count} approved users")

    if not lines:
        await update.effective_chat.send_message("No data yet.")
        return

    text = "\n".join(lines)
    # Telegram limit ~4096 chars; split if needed
    if len(text) <= 4000:
        await update.effective_chat.send_message(text)
    else:
        chunk_size = 3500
        for i in range(0, len(text), chunk_size):
            await update.effective_chat.send_message(text[i : i + chunk_size])


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_chat.send_message("You are not authorized to use this command.")
        return

    message = " ".join(context.args).strip()
    if not message:
        await update.effective_chat.send_message("Usage: /broadcast Your message here")
        return

    all_user_ids = set()
    for info in DATA.get("channels", {}).values():
        for uid in info.get("users", []):
            all_user_ids.add(uid)

    sent = 0
    failed = 0
    for uid in all_user_ids:
        try:
            await context.bot.send_message(chat_id=int(uid), text=message)
            sent += 1
        except Exception as e:
            logger.warning("Failed to send broadcast to %s: %s", uid, e)
            failed += 1

    await update.effective_chat.send_message(f"Broadcast complete. Sent: {sent}, Failed: {failed}")


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles ChatJoinRequest updates:
    - Approves the request
    - Stores user id under the chat in DATA
    - Sends welcome + promotion to the user (if possible)
    - Logs approval to DATA_CHANNEL_ID and LOG_CHANNEL_ID (if set)
    """
    req: ChatJoinRequest = update.chat_join_request
    user = req.from_user
    chat: Chat = req.chat

    logger.info(
        "Join request from user=%s (%s) for chat=%s (%s)",
        user.id,
        user.full_name,
        chat.id,
        chat.title,
    )

    # Try to approve
    try:
        await context.bot.approve_chat_join_request(chat_id=chat.id, user_id=user.id)
    except Exception as e:
        logger.exception("Failed to approve join request: %s", e)
        return

    # Update local datastore
    cid = str(chat.id)
    chinfo = DATA.setdefault("channels", {}).setdefault(
        cid, {"title": chat.title or str(chat.id), "users": []}
    )
    if user.id not in chinfo["users"]:
        chinfo["users"].append(user.id)
    save_data()

    # Prepare and DM welcome + promotion
    welcome = f"Welcome, {user.first_name}!\nYou have been approved to join {chat.title or 'the channel/group'}."
    promo = DATA.get("promotion", "")
    text_to_user = welcome + ("\n\n" + promo if promo else "")

    try:
        await context.bot.send_message(chat_id=user.id, text=text_to_user)
    except Exception as e:
        # Common: bot cannot message user who hasn't started the bot
        logger.warning("Could not DM user %s: %s", user.id, e)

    # Log to DATA_CHANNEL_ID (database channel)
    if DATA_CHANNEL_ID:
        username = f"@{user.username}" if user.username else "None"
        log_text = (
            "🔔 New Join Request Approved\n\n"
            f"👤 User: {user.full_name}\n"
            f"🆔 User ID: {user.id}\n"
            f"🎭 Username: {username}\n"
            f"🏷️ Channel : {chat.title or 'Unknown'}\n"
            f"🗨️ Chat ID: {chat.id}"
        )
        try:
            await context.bot.send_message(chat_id=DATA_CHANNEL_ID, text=log_text)
        except Exception as e:
            logger.exception("Failed to send log to data channel: %s", e)

    # Optional admin log channel
    if LOG_CHANNEL_ID:
        try:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=f"Auto-approved {user.full_name} ({user.id}) in {chat.title}",
            )
        except Exception as e:
            logger.exception("Failed to send log to log channel: %s", e)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message("Unknown command.")


def main() -> None:
    load_data()

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set. Exiting.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("promotion", set_promotion))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("details", details_cmd))

    # Chat join requests
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Starting bot...")
    app.run_polling()


if __name__ == "__main__":
    main()
