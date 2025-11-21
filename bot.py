import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Set

from dotenv import load_dotenv
from telegram import Update, ChatJoinRequest
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    ContextTypes,
    ChatJoinRequestHandler,
    filters,
)

# ----------------------------------
# Environment and configuration
# ----------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
DATA_CHANNEL_ID = int(os.getenv("DATA_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
PERSIST_FILE = os.getenv("PERSIST_FILE", "data.json").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required. Set it in .env.")

# ----------------------------------
# Logging
# ----------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("auto_approve_bot")

# ----------------------------------
# Persistence helpers (JSON)
# Structure:
# {
#   "promotion_message": "text or empty",
#   "chats": {
#       "<chat_id>": {
#           "title": "<chat_title>",
#           "users": [
#               {"id": <int>, "full_name": "<str>", "username": "<str or None>", "approved_at": "<ISO8601>"}
#           ]
#       }
#   }
# }
# ----------------------------------

def load_data() -> Dict[str, Any]:
    if not os.path.exists(PERSIST_FILE):
        return {"promotion_message": "", "chats": {}}
    try:
        with open(PERSIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "promotion_message" not in data:
                data["promotion_message"] = ""
            if "chats" not in data:
                data["chats"] = {}
            return data
    except Exception as e:
        logger.error(f"Failed to load {PERSIST_FILE}: {e}")
        return {"promotion_message": "", "chats": {}}

def save_data(data: Dict[str, Any]) -> None:
    try:
        with open(PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save {PERSIST_FILE}: {e}")

DATA = load_data()
DATA_LOCK = asyncio.Lock()

# ----------------------------------
# Utility
# ----------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def safe_send_log(app: Application, text: str) -> None:
    if LOG_CHANNEL_ID:
        try:
            await app.bot.send_message(LOG_CHANNEL_ID, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to send log to LOG_CHANNEL_ID: {e}")

# ----------------------------------
# Command handlers
# ----------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Hello!\n\n"
        "This is an Auto-Approve Bot for Channel/Group Join Requests.\n"
        "Features:\n"
        "• Auto approves join requests\n"
        "• Welcomes the user and sends your promotion\n"
        "• Logs approvals to your data channel\n"
        "• Stores approved users in JSON (no database)\n\n"
        "Admin commands:\n"
        "• /users – total stored approved users\n"
        "• /details – channel-wise approved users\n"
        "• /promotion <text> – set promotion message\n"
        "• /broadcast <text> – send a message to all saved users\n"
    )
    await update.effective_message.reply_text(text)

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    async with DATA_LOCK:
        total = sum(len(info.get("users", [])) for info in DATA.get("chats", {}).values())
    await update.effective_message.reply_text(f"📦 Total approved users stored: {total}")

async def details_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    async with DATA_LOCK:
        chats = DATA.get("chats", {})
        if not chats:
            await update.effective_message.reply_text("ℹ️ No data yet.")
            return
        lines: List[str] = []
        for chat_id, info in chats.items():
            title = info.get("title") or str(chat_id)
            count = len(info.get("users", []))
            lines.append(f"• {title} ({chat_id}): {count} users")
    await update.effective_message.reply_text("📊 Channel-wise details:\n" + "\n".join(lines))

async def promotion_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    text = (context.args and " ".join(context.args).strip()) if context.args else ""
    async with DATA_LOCK:
        DATA["promotion_message"] = text or ""
        save_data(DATA)
    if text:
        await update.effective_message.reply_text("✅ Promotion message updated.")
    else:
        await update.effective_message.reply_text("✅ Promotion message cleared.")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
    msg = (context.args and " ".join(context.args).strip()) if context.args else ""
    if not msg:
        await update.effective_message.reply_text("❗ Usage: /broadcast <text>")
        return

    # Collect unique user IDs across all chats
    async with DATA_LOCK:
        chats = DATA.get("chats", {})
        recipients: Set[int] = set()
        for info in chats.values():
            for u in info.get("users", []):
                uid = u.get("id")
                if isinstance(uid, int):
                    recipients.add(uid)

    await update.effective_message.reply_text(f"🚀 Broadcasting to {len(recipients)} users...")

    sent = 0
    failed = 0
    for uid in recipients:
        try:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            sent += 1
            await asyncio.sleep(0.02)  # gentle pacing
        except Exception:
            failed += 1
            continue

    await update.effective_message.reply_text(f"✅ Broadcast finished. Sent: {sent}, Failed: {failed}")

# ----------------------------------
# Join request handler
# ----------------------------------

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req: ChatJoinRequest = update.chat_join_request
    app = context.application

    try:
        # Approve request
        await req.approve()

        user = req.from_user
        chat = req.chat

        full_name = (user.full_name or "").strip() if user else "Unknown"
        username = user.username if user and user.username else None
        user_id = user.id if user else 0
        chat_id = chat.id if chat else 0
        channel_title = (chat.title or "").strip() if chat else "Unknown"
        approved_at = datetime.utcnow().isoformat()

        # Persist user to JSON
        async with DATA_LOCK:
            chats = DATA.setdefault("chats", {})
            chat_entry = chats.setdefault(str(chat_id), {"title": channel_title, "users": []})
            # Update title if changed
            chat_entry["title"] = channel_title

            # Prevent duplicates
            exists = any(u.get("id") == user_id for u in chat_entry["users"])
            if not exists and user_id:
                chat_entry["users"].append(
                    {"id": user_id, "full_name": full_name, "username": username, "approved_at": approved_at}
                )
            save_data(DATA)

        # DM welcome and promotion
        welcome_text = (
            "🎉 You’re in!\n\n"
            f"Welcome to {channel_title}.\n"
            "We’ve approved your join request—enjoy the content!"
        )
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=welcome_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"Failed to send welcome DM to {user_id}: {e}")

        # Promotion message (if set)
        async with DATA_LOCK:
            promo = DATA.get("promotion_message", "").strip()
        if promo:
            try:
                await app.bot.send_message(
                    chat_id=user_id,
                    text=promo,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(f"Failed to send promo DM to {user_id}: {e}")

        # Log to Data Channel
        if DATA_CHANNEL_ID:
            log_text = (
                "🔔 New Join Request Approved\n\n"
                f"👤 User: {full_name}\n"
                f"🆔 ID: {user_id}\n"
                f"🎭 Username: {username if username else 'None'}\n"
                f"🏷️ Channel: {channel_title}\n"
                f"💬 Chat ID: {chat_id}"
            )
            try:
                await app.bot.send_message(
                    chat_id=DATA_CHANNEL_ID,
                    text=log_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Failed to send approval log to DATA_CHANNEL_ID: {e}")

    except Exception as e:
        logger.exception(f"Error handling join request: {e}")
        await safe_send_log(context.application, f"❗ Error handling join request:\n<code>{e}</code>")

# ----------------------------------
# Error handler (optional internal logs)
# ----------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception", exc_info=context.error)
    await safe_send_log(context.application, f"❗ Unhandled exception:\n<code>{context.error}</code>")

# ----------------------------------
# Application setup
# ----------------------------------

def main() -> None:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("users", users_cmd, filters=filters.User(ADMIN_IDS)))
    application.add_handler(CommandHandler("details", details_cmd, filters=filters.User(ADMIN_IDS)))
    application.add_handler(CommandHandler("promotion", promotion_cmd, filters=filters.User(ADMIN_IDS)))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd, filters=filters.User(ADMIN_IDS)))

    # Auto-approve join requests
    application.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Error handler
    application.add_error_handler(error_handler)

    # Run polling (no Updater, PTB >= 20)
    application.run_polling(allowed_updates=["chat_join_request"])

if __name__ == "__main__":
    main()
