#!/usr/bin/env python3
"""
Advanced Auto-Approve Bot (full, corrected)

Behavior:
- Auto-approve chat join requests
- If the user has started the bot: DM a welcome + promo message on approval
- If DM fails (user didn't start the bot / forbidden): post a welcome + promo in the channel/group, mentioning the user
- Log each approval to DATA_CHANNEL_ID (acts as your "database channel")
- Persist approvals in a JSON file (PERSIST_FILE)
"""

import os
import json
import logging
import asyncio
import html
from datetime import datetime
from typing import Dict, Any, List, Optional, Set

from dotenv import load_dotenv
from telegram import Update, Chat, ChatJoinRequest
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatJoinRequestHandler,
    MessageHandler,
    filters,
)

# ------------
# Load env
# ------------
load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW: str = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: List[int] = []
for part in ADMIN_IDS_RAW.split(","):
    p = part.strip()
    if not p:
        continue
    try:
        ADMIN_IDS.append(int(p))
    except ValueError:
        # ignore invalid
        pass

DATA_CHANNEL_ID: int = int(os.getenv("DATA_CHANNEL_ID", "0") or 0)
LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
PERSIST_FILE: str = os.getenv("PERSIST_FILE", "data.json").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required. Set it in .env")

# ------------
# Logging
# ------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("auto_approve_bot")

# ------------
# Persistence
# ------------
# DATA structure:
# {
#   "promotion_message": "",
#   "chats": {
#       "<chat_id>": {
#           "title": "<chat_title>",
#           "users": [
#               {"id": 123, "full_name": "Name", "username": "name", "approved_at": "<iso>"}
#           ]
#       }
#   }
# }

DATA: Dict[str, Any] = {"promotion_message": "", "chats": {}}
DATA_LOCK = asyncio.Lock()


def load_data() -> None:
    global DATA
    if not os.path.exists(PERSIST_FILE):
        DATA = {"promotion_message": "", "chats": {}}
        return
    try:
        with open(PERSIST_FILE, "r", encoding="utf-8") as f:
            DATA = json.load(f)
            if "promotion_message" not in DATA:
                DATA["promotion_message"] = ""
            if "chats" not in DATA:
                DATA["chats"] = {}
            logger.info("Loaded data from %s", PERSIST_FILE)
    except Exception as e:
        logger.error("Failed to load %s: %s", PERSIST_FILE, e)
        DATA = {"promotion_message": "", "chats": {}}


def save_data() -> None:
    try:
        with open(PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Failed to save %s: %s", PERSIST_FILE, e)


# ------------
# Helpers
# ------------
def is_admin(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    return user_id in ADMIN_IDS


async def safe_send_log(application, text: str) -> None:
    if not LOG_CHANNEL_ID:
        return
    try:
        await application.bot.send_message(
            chat_id=LOG_CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception as e:
        logger.error("Failed to send admin log: %s", e)


def html_escape(s: Optional[str]) -> str:
    return html.escape(s or "")


# ------------
# Commands
# ------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Hello!\n\n"
        "This bot auto-approves channel/group join requests (if bot is admin), "
        "DMs welcome+promo when possible, and logs approvals to your data channel.\n\n"
        "Admin commands:\n"
        "/users - Total approved users stored\n"
        "/details - Channel-wise approved users\n"
        "/promotion <text> - Set promotion message\n"
        "/broadcast <text> - Broadcast to all stored users\n"
    )
    await update.effective_message.reply_text(text)


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        return
    async with DATA_LOCK:
        total = sum(len(chat.get("users", [])) for chat in DATA.get("chats", {}).values())
    await update.effective_message.reply_text(f"📦 Total approved users stored: {total}")


async def details_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        return
    async with DATA_LOCK:
        chats = DATA.get("chats", {})
        if not chats:
            await update.effective_message.reply_text("ℹ️ No data yet.")
            return
        lines: List[str] = []
        for cid, info in chats.items():
            title = info.get("title") or str(cid)
            count = len(info.get("users", []))
            lines.append(f"• {title} ({cid}): {count} users")
    await update.effective_message.reply_text("📊 Channel-wise details:\n" + "\n".join(lines))


async def promotion_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        return
    msg = " ".join(context.args).strip() if context.args else ""
    async with DATA_LOCK:
        DATA["promotion_message"] = msg or ""
        save_data()
    await update.effective_message.reply_text("✅ Promotion message updated." if msg else "✅ Promotion message cleared.")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        return
    msg = " ".join(context.args).strip() if context.args else ""
    if not msg:
        await update.effective_message.reply_text("❗ Usage: /broadcast <text>")
        return

    # Collect recipients
    async with DATA_LOCK:
        recipients: Set[int] = set()
        for info in DATA.get("chats", {}).values():
            for user in info.get("users", []):
                uid = user.get("id")
                if isinstance(uid, int):
                    recipients.add(uid)

    await update.effective_message.reply_text(f"🚀 Broadcasting to {len(recipients)} users...")
    sent = 0
    failed = 0
    for uid in recipients:
        try:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            sent += 1
            await asyncio.sleep(0.02)
        except Exception:
            failed += 1
    await update.effective_message.reply_text(f"✅ Broadcast finished. Sent: {sent}, Failed: {failed}")


# ------------
# Join request handler (DM if possible; otherwise post in channel with mention)
# ------------
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req: ChatJoinRequest = update.chat_join_request
    app = context.application

    try:
        # Approve join request using bot API (safer than req.approve() cross-version)
        try:
            await context.bot.approve_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)
        except Exception as e:
            logger.exception("Failed to approve request via API: %s", e)
            # still continue to try logging/notify

        user = req.from_user
        chat = req.chat

        full_name = (user.full_name or "").strip() if user else "Unknown"
        username = user.username if user and user.username else None
        user_id = user.id if user else 0
        chat_id = chat.id if chat else 0
        channel_title = (chat.title or "").strip() if chat else "Unknown"
        approved_at = datetime.utcnow().isoformat()

        # Persist the approval
        async with DATA_LOCK:
            chats = DATA.setdefault("chats", {})
            chat_entry = chats.setdefault(str(chat_id), {"title": channel_title, "users": []})
            chat_entry["title"] = channel_title  # update if changed
            exists = any(u.get("id") == user_id for u in chat_entry["users"])
            if not exists and user_id:
                chat_entry["users"].append(
                    {"id": user_id, "full_name": full_name, "username": username, "approved_at": approved_at}
                )
            save_data()

        # Prepare messages
        welcome_text = f"🎉 You’re in!\n\nWelcome to {channel_title}.\nWe’ve approved your join request — enjoy the content!"
        async with DATA_LOCK:
            promo = DATA.get("promotion_message", "").strip()

        # Try DM first
        dm_ok = False
        try:
            if user_id:
                await app.bot.send_message(chat_id=user_id, text=welcome_text + ("\n\n" + promo if promo else ""),
                                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                dm_ok = True
        except Exception as e:
            logger.info("Could not DM user %s: %s", user_id, e)
            dm_ok = False

        # If DM failed, post in the channel with mention
        if not dm_ok:
            mention = f'<a href="tg://user?id={user_id}">{html_escape(full_name)}</a>'
            channel_msg = f"🎉 {mention} has been approved to join <b>{html_escape(channel_title)}</b>."
            if promo:
                channel_msg += f"\n\n{promo}"
            try:
                await app.bot.send_message(chat_id=chat_id, text=channel_msg,
                                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as e:
                logger.error("Failed to post welcome message in chat %s: %s", chat_id, e)

        # Log to DATA channel
        if DATA_CHANNEL_ID:
            username_str = f"@{username}" if username else "None"
            log_text = (
                "🔔 New Join Request Approved\n\n"
                f"👤 User: {html_escape(full_name)}\n"
                f"🆔 ID: {user_id}\n"
                f"🎭 Username: {html_escape(username_str)}\n"
                f"🏷️ Channel: {html_escape(channel_title)}\n"
                f"🗨️ Chat ID: {chat_id}"
            )
            try:
                await app.bot.send_message(chat_id=DATA_CHANNEL_ID, text=log_text,
                                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as e:
                logger.error("Failed to send log to DATA_CHANNEL_ID: %s", e)

    except Exception as e:
        logger.exception("Error handling join request: %s", e)
        await safe_send_log(app, f"❗ Error handling join request:\n<code>{html_escape(str(e))}</code>")


# ------------
# Error handler
# ------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
    await safe_send_log(context.application, f"❗ Unhandled exception:\n<code>{html_escape(str(context.error))}</code>")


# ------------
# App entry
# ------------
def main() -> None:
    load_data()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("users", users_cmd, filters=filters.User(ADMIN_IDS)))
    app.add_handler(CommandHandler("details", details_cmd, filters=filters.User(ADMIN_IDS)))
    app.add_handler(CommandHandler("promotion", promotion_cmd, filters=filters.User(ADMIN_IDS)))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd, filters=filters.User(ADMIN_IDS)))

    # Chat join request handler
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("Bot starting — run_polling (listening for chat_join_request updates)...")
    # Ask PTB to include chat_join_request updates
    app.run_polling(allowed_updates=["chat_join_request", "message", "edited_message", "callback_query"])

if __name__ == "__main__":
    main()
