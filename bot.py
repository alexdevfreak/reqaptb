#!/usr/bin/env python3
"""
Advanced Auto-Approve Bot — robust, production-ready.

Features:
- Auto-approve chat join requests
- DM welcome + promotion if user has started bot
- Fallback to posting a mention in the channel/group if DM fails
- Log approvals to DATA_CHANNEL_ID
- Persist approvals to a JSON file (PERSIST_FILE)
- Admin commands: /start, /users, /details, /promotion, /broadcast
- Retries on Telegram 409 Conflict (another getUpdates instance)
"""

import os
import json
import logging
import asyncio
import html
from datetime import datetime
from typing import Dict, Any, List, Optional, Set

from dotenv import load_dotenv
from telegram import Update, ChatJoinRequest
from telegram.constants import ParseMode
from telegram import error as tg_error
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatJoinRequestHandler,
    MessageHandler,
    filters,
)

# -------------------------
# Load environment
# -------------------------
load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW: str = os.getenv("ADMIN_IDS", "").strip()
DATA_CHANNEL_ID: int = int(os.getenv("DATA_CHANNEL_ID", "0") or 0)
LOG_CHANNEL_ID: int = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
PERSIST_FILE: str = os.getenv("PERSIST_FILE", "data.json").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required. Set it in environment or .env")

# parse admins safely
ADMIN_IDS: List[int] = []
for part in ADMIN_IDS_RAW.split(","):
    p = part.strip()
    if not p:
        continue
    try:
        ADMIN_IDS.append(int(p))
    except ValueError:
        # ignore invalid entries
        pass

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("auto_approve_bot")

# -------------------------
# Persistence (JSON)
# -------------------------
# DATA structure:
# {
#   "promotion_message": "",
#   "chats": {
#       "<chat_id>": {
#           "title": "<chat_title>",
#           "users": [ { "id": int, "full_name": str, "username": str|None, "approved_at": iso } ]
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
        logger.warning("Could not load %s: %s", PERSIST_FILE, e)
        DATA = {"promotion_message": "", "chats": {}}


def save_data() -> None:
    try:
        with open(PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Failed to save %s: %s", PERSIST_FILE, e)


# -------------------------
# Helpers
# -------------------------
def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


async def safe_send_log(application, text: str) -> None:
    if not LOG_CHANNEL_ID:
        return
    try:
        await application.bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning("safe_send_log failed: %s", e)


def html_escape(s: Optional[str]) -> str:
    return html.escape(s or "")


# -------------------------
# Command handlers
# -------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Hello!\n\n"
        "This bot auto-approves join requests (if the bot is admin in the group/channel), "
        "DMs welcome+promo when possible, logs approvals to your data channel and stores users in JSON.\n\n"
        "Admin commands:\n"
        "/users - total approved users stored\n"
        "/details - channel-wise approved users\n"
        "/promotion <text> - set promotion message\n"
        "/broadcast <text> - broadcast to all stored users\n"
    )
    await update.effective_message.reply_text(text)


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or not is_admin(u.id):
        return
    async with DATA_LOCK:
        total = sum(len(c.get("users", [])) for c in DATA.get("chats", {}).values())
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

    async with DATA_LOCK:
        recipients: Set[int] = set()
        for info in DATA.get("chats", {}).values():
            for uinfo in info.get("users", []):
                uid = uinfo.get("id")
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


# -------------------------
# Join request handler (DM if possible; fallback to channel mention)
# -------------------------
async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    req: ChatJoinRequest = update.chat_join_request
    app = context.application

    try:
        # Approve via API call (works reliably)
        try:
            await context.bot.approve_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)
        except Exception as e:
            logger.warning("approve_chat_join_request API call failed: %s", e)

        user = req.from_user
        chat = req.chat

        full_name = (user.full_name or "").strip() if user else "Unknown"
        username = user.username if user and user.username else None
        user_id = user.id if user else 0
        chat_id = chat.id if chat else 0
        channel_title = (chat.title or "").strip() if chat else "Unknown"
        approved_at = datetime.utcnow().isoformat()

        # Persist
        async with DATA_LOCK:
            chats = DATA.setdefault("chats", {})
            chat_entry = chats.setdefault(str(chat_id), {"title": channel_title, "users": []})
            chat_entry["title"] = channel_title
            exists = any(u.get("id") == user_id for u in chat_entry["users"])
            if not exists and user_id:
                chat_entry["users"].append({"id": user_id, "full_name": full_name, "username": username, "approved_at": approved_at})
            save_data()

        # Prepare messages
        welcome_text = f"🎉 You’re in!\n\nWelcome to {channel_title}.\nWe’ve approved your join request — enjoy the content!"
        async with DATA_LOCK:
            promo = DATA.get("promotion_message", "").strip()

        # Try DM first
        dm_ok = False
        if user_id:
            try:
                await app.bot.send_message(chat_id=user_id, text=welcome_text + ("\n\n" + promo if promo else ""),
                                           parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                dm_ok = True
            except Exception as e:
                logger.info("Could not DM user %s: %s", user_id, e)

        # Fallback: post in the chat with mention
        if not dm_ok:
            mention = f'<a href="tg://user?id={user_id}">{html_escape(full_name)}</a>'
            channel_msg = f"🎉 {mention} has been approved to join <b>{html_escape(channel_title)}</b>."
            if promo:
                channel_msg += f"\n\n{promo}"
            try:
                await app.bot.send_message(chat_id=chat_id, text=channel_msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as e:
                logger.error("Failed to post fallback welcome to chat %s: %s", chat_id, e)

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
                await app.bot.send_message(chat_id=DATA_CHANNEL_ID, text=log_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as e:
                logger.error("Failed to send approval log to DATA_CHANNEL_ID: %s", e)

    except Exception as e:
        logger.exception("Error handling join request: %s", e)
        await safe_send_log(app, f"❗ Error handling join request:\n<code>{html_escape(str(e))}</code>")


# -------------------------
# Error handler
# -------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Log the error and alert admin log
    logger.exception("Unhandled exception: %s", context.error)
    await safe_send_log(context.application, f"❗ Unhandled exception:\n<code>{html_escape(str(context.error))}</code>")


# -------------------------
# App runtime: robust runner with Conflict retry/backoff
# -------------------------
async def _runner(app):
    backoff = 5
    while True:
        try:
            logger.info("Starting Application.run_polling()...")
            # run_polling is an async coroutine in modern PTB; await it
            await app.run_polling()
            logger.info("run_polling exited normally.")
            return
        except tg_error.Conflict as e:
            logger.warning("Telegram Conflict (another getUpdates running): %s", e)
            await safe_send_log(app, f"⚠️ Conflict: another getUpdates instance. Retrying in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
        except Exception as e:
            logger.exception("Unexpected error in run loop: %s", e)
            await safe_send_log(app, f"❗ Unexpected error: {html_escape(str(e))}\nRetrying in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


def main() -> None:
    load_data()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("users", users_cmd, filters=filters.User(ADMIN_IDS)))
    app.add_handler(CommandHandler("details", details_cmd, filters=filters.User(ADMIN_IDS)))
    app.add_handler(CommandHandler("promotion", promotion_cmd, filters=filters.User(ADMIN_IDS)))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd, filters=filters.User(ADMIN_IDS)))

    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    app.add_error_handler(error_handler)

    # Run runner in asyncio
    try:
        asyncio.run(_runner(app))
    except KeyboardInterrupt:
        logger.info("Interrupted by user, exiting.")
    except Exception as e:
        logger.exception("Fatal error in main: %s", e)


if __name__ == "__main__":
    main()
