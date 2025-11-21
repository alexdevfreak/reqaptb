import os
import json
import logging
from typing import Dict, Any
from dotenv import load_dotenv
from telegram import Update, ChatJoinRequest, Chat
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DATA_CHANNEL_ID = int(os.getenv("DATA_CHANNEL_ID") or 0)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID") or 0)
PERSIST_FILE = os.getenv("PERSIST_FILE", "data.json")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DATA: Dict[str, Any] = {"channels": {}, "promotion": ""}


def load_data():
    global DATA
    if os.path.exists(PERSIST_FILE):
        try:
            with open(PERSIST_FILE, "r", encoding="utf-8") as f:
                DATA = json.load(f)
        except:
            pass


def save_data():
    try:
        with open(PERSIST_FILE, "w", encoding="utf-8") as f:
            json.dump(DATA, f, indent=2)
    except:
        pass


def is_admin(uid):
    return uid in ADMIN_IDS


# ------------------------ COMMANDS ------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Hi! I am your Auto Approve Bot.\n\n"
        "Admin Commands:\n"
        "/promotion <msg>\n"
        "/users\n"
        "/details\n"
        "/broadcast <msg>\n"
    )
    await update.message.reply_text(txt)


async def promotion_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Not allowed.")

    msg = " ".join(context.args).strip()
    if not msg:
        return await update.message.reply_text("Usage: /promotion <your message>")

    DATA["promotion"] = msg
    save_data()
    await update.message.reply_text("Promotion message saved!")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Not allowed.")

    total = sum(len(v["users"]) for v in DATA["channels"].values())
    await update.message.reply_text(f"Total approved users: {total}")


async def details_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Not allowed.")

    msg = ""
    for cid, info in DATA["channels"].items():
        msg += f"{info['title']} ({cid}) — {len(info['users'])} users\n"

    if not msg:
        msg = "No data yet."

    await update.message.reply_text(msg)


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Not allowed.")

    message = " ".join(context.args).strip()
    if not message:
        return await update.message.reply_text("Usage: /broadcast <message>")

    all_users = set()
    for info in DATA["channels"].values():
        all_users.update(info["users"])

    sent = 0
    fail = 0
    for uid in all_users:
        try:
            await context.bot.send_message(uid, message)
            sent += 1
        except:
            fail += 1

    await update.message.reply_text(f"Broadcast done.\nSent: {sent}\nFailed: {fail}")


# ------------------------ JOIN REQUEST ------------------------

async def approve_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req: ChatJoinRequest = update.chat_join_request
    user = req.from_user
    chat: Chat = req.chat

    # Approve
    await context.bot.approve_chat_join_request(chat.id, user.id)

    # Save data
    cid = str(chat.id)
    if cid not in DATA["channels"]:
        DATA["channels"][cid] = {"title": chat.title, "users": []}

    if user.id not in DATA["channels"][cid]["users"]:
        DATA["channels"][cid]["users"].append(user.id)

    save_data()

    # User DM
    promo = DATA["promotion"]
    msg = f"Welcome {user.first_name}! Approved for {chat.title}."
    if promo:
        msg += f"\n\n{promo}"

    try:
        await context.bot.send_message(user.id, msg)
    except:
        pass

    # Log to database channel
    if DATA_CHANNEL_ID:
        username = f"@{user.username}" if user.username else "None"
        log_msg = (
            "🔔 New Join Approved\n\n"
            f"👤 {user.full_name}\n"
            f"🆔 {user.id}\n"
            f"🎭 Username: {username}\n"
            f"🏷️ Channel: {chat.title}\n"
            f"💬 Chat ID: {chat.id}"
        )
        try:
            await context.bot.send_message(DATA_CHANNEL_ID, log_msg)
        except:
            pass


# ------------------------ UNKNOWN COMMAND ------------------------

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command.")


# ------------------------ MAIN ------------------------

def main():
    load_data()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("promotion", promotion_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("details", details_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Join request
    app.add_handler(ChatJoinRequestHandler(approve_request))

    # Unknown
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("Bot running on Render...")
    app.run_polling()


if __name__ == "__main__":
    main()
