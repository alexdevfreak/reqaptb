#############################
#  REPO: AutoApproveBot
#  Files included below:
#   - .env.example
#   - Procfile
#   - requirements.txt
#   - bot.py
#   - README.md
#
# Paste each file's contents into its respective file in your GitHub repo.
#############################

==== FILE: .env.example ====
# Copy this to a file named ".env" and edit values (DO NOT commit secrets)
BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
ADMIN_IDS=123456789,987654321            # comma-separated admin user IDs
DATA_CHANNEL_ID=-1001234567890           # channel ID where approval logs are posted
LOG_CHANNEL_ID=-1009876543210            # optional admin log channel (0 or empty to disable)
PERSIST_FILE=data.json                   # file to persist counts (default data.json)

==== FILE: Procfile ====
web: python bot.py

==== FILE: requirements.txt ====
python-telegram-bot==20.5
python-dotenv==1.0.0

==== FILE: bot.py ====
"""
bot.py - Advanced Telegram Channel/Group Auto-Approve Bot
Requires: python-telegram-bot v20+, python-dotenv

Features:
- Auto-approve chat join requests
- Send welcome + optional promotion message
- Keep per-channel approved-user tracking (persisted to JSON)
- Log approved users to a DATA_CHANNEL_ID (acts as database channel)
- Admin commands: /start, /promotion, /users, /details, /broadcast
"""

import os
import json
import logging
from typing import Dict, Any
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

# Load .env variables if present
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID") or 0)
DATA_CHANNEL_ID = int(os.getenv("DATA_CHANNEL_ID") or 0)
PERSIST_FILE = os.getenv("PERSIST_FILE", "data.json")

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory data; persisted to PERSIST_FILE
DATA: Dict[str, Any] = {
    "channels": {},  # chat_id_str -> {"title": str, "users": [user_id,...]}
    "promotion": "",  # global promotion message (admin-set)
}

def load_data() -> None:
    global DATA
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r", encoding="utf-8") as f:
                DATA = json.load(f)
                logger.info("Loaded data from %s", PERSIST_FILE)
    except Exception as e:
        logger.exception("Failed to load data: %s", e)

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
        "Hi — I'm your Auto-Approve Bot.\n\n"
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
    for c in DATA.get("channels", {}).values():
        total += len(c.get("users", []))
    await update.effective_chat.send_message(f"Total stored users: {total}")

async def details_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.effective_chat.send_message("You are not authorized to use this command.")
        return
    lines = []
    for cid, info in DATA.get("channels", {}).items():
        title = info.get("title") or str(cid)
        count = len(info.get("users", []))
        lines.append(f"{title} ({cid}) - {count} approved users")
    if not lines:
        await update.effective_chat.send_message("No data yet.")
        return
    # send as multiple messages if too long
    text = "\n".join(lines)
    if len(text) > 4000:
        # split into chunks
        parts = [text[i:i+3500] for i in range(0, len(text), 3500)]
        for p in parts:
            await update.effective_chat.send_message(p)
    else:
        await update.effective_chat.send_message(text)

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
    for c in DATA.get("channels", {}).values():
        for uid in c.get("users", []):
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
    # ChatJoinRequest handler
    req: ChatJoinRequest = update.chat_join_request
    user = req.from_user
    chat: Chat = req.chat

    logger.info("New join request from %s (%s) for chat %s (%s)", user.id, user.full_name, chat.id, chat.title)

    try:
        await context.bot.approve_chat_join_request(chat_id=chat.id, user_id=user.id)
    except Exception as e:
        logger.exception("Failed to approve join request: %s", e)
        return

    # Update data store
    cid = str(chat.id)
    chinfo = DATA.setdefault("channels", {}).setdefault(cid, {"title": chat.title or str(chat.id), "users": []})
    if user.id not in chinfo["users"]:
        chinfo["users"].append(user.id)
    save_data()

    # Compose welcome + promotion
    welcome = f"Welcome, {user.first_name}!\nYou have been approved to join {chat.title or 'the channel/group'}."
    promo = DATA.get("promotion")
    text_to_user = welcome + ("\n\n" + promo if promo else "")

    # Try to DM the user (may fail if user hasn't started bot)
    try:
        await context.bot.send_message(chat_id=user.id, text=text_to_user)
    except Exception as e:
        logger.warning("Could not DM user %s: %s", user.id, e)

    # Log to DATA_CHANNEL_ID - acts as database channel
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
            await context.bot.send_message(chat_id=LOG_CHANNEL_ID, text=f"Auto-approved {user.full_name} ({user.id}) in {chat.title}")
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

    # Register handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("promotion", set_promotion))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("details", details_cmd))

    # Join request handler
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()

==== FILE: README.md ====
# 🚀 Advanced Telegram Auto Request Accept Bot

<h2 align="center">──「 ᴀᴅᴠᴀɴᴄᴇᴅ ᴀᴜᴛᴏ ᴀᴘᴘʀᴏᴠᴇ ʙᴏᴛ 」──</h2>

<p align="center">
  <img src="https://graph.org/file/8581e33195ed8183a3253.jpg">
</p>

<p align="center">
<img src="https://readme-typing-svg.herokuapp.com/?lines=ADVANCED+AUTO+APPROVE+BOT!;CREATED+BY+AYUSH+%7C+ALEXDEVFREAK!;A+POWERFUL+TG+APPROVAL+BOT!"/>
</p>

━━━━━━━━━━━━━━━━━━━━

## 🔥 Features

<details><summary><b>Click to Expand</b></summary>

### ✅ Core Auto-Approval System
- Automatically **approves Join Requests** from Channels & Groups.
- Sends **Welcome Message** to each approved user.
- Sends **Admin-set Promotional Message** along with welcome.
- Maintains **per-channel user approval tracking**.

### 📦 Logging System
- Logs each approved user in the **Database Channel**.
- Log contains:
  - User Name  
  - User ID  
  - Username  
  - Channel / Group Name  
  - Chat ID  

### 👑 Admin Commands
- `/promotion <text>` → Save promotional message  
- `/users` → Show total approved users  
- `/details` → Show channel-wise approval list  
- `/broadcast <text>` → Message all users  

### 💾 Database System
- No MongoDB — uses a **JSON local database** + **Database Channel**.

### ⚡ Light & Fast  
- Perfect for **Railway**, **Heroku**, **Koyeb**, **Render**, **VPS**.

</details>

━━━━━━━━━━━━━━━━━━━━

## 🔧 Environment Variables (Config)


━━━━━━━━━━━━━━━━━━━━

## 🛠️ Commands


━━━━━━━━━━━━━━━━━━━━

## 🚀 Deployment Methods

<details><summary><b>Deploy on Heroku</b></summary>
<p align="center"><a href="https://heroku.com/deploy?template=https://github.com/alexdevfreak/FileStore"><img src="https://www.herokucdn.com/deploy/button.svg"></a></p>
</details>

<details><summary><b>Deploy on Koyeb</b></summary>
<p align="center"><a href="https://app.koyeb.com/deploy?type=git&repository=github.com/alexdevfreak/FileStore&branch=master"><img src="https://www.koyeb.com/static/images/deploy/button.svg"></a></p>
</details>

<details><summary><b>Deploy on Railway</b></summary>
<p align="center"><a href="https://railway.app/deploy?template=https://github.com/alexdevfreak/FileStore"><img height="45px" src="https://railway.app/button.svg"></a></p>
</details>

<details><summary><b>Deploy on Render</b></summary>
<p align="center"><a href="https://render.com/deploy?repo=https://github.com/alexdevfreak/FileStore"><img src="https://render.com/images/deploy-to-render-button.svg"></a></p>
</details>

<details><summary><b>Deploy on VPS</b></summary>


</details>

━━━━━━━━━━━━━━━━━━━━

## 🖤 Credits

- **Ayush** (AlexDevFreak) – Developer  
- Contributors and testers who helped polish this bot.

━━━━━━━━━━━━━━━━━━━━

## 📌 Note

💡 *Just fork the repo and edit as per your needs.*

#############################
# End of bundled files
#############################
