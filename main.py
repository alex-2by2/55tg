# main.py
import os
import logging
import sqlite3
from typing import List, Optional
from urllib.parse import quote_plus, urlparse
import requests

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ---------------- CONFIG via ENV ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # REQUIRED
SOURCE_CHANNEL_ID = os.environ.get("SOURCE_CHANNEL_ID", "")  # optional: restrict source
DEST_CHANNELS = os.environ.get("DEST_CHANNELS", "")  # comma-separated (REQUIRED)
REDIRECT_BASE = os.environ.get("REDIRECT_BASE", "")  # e.g. https://go.example.com (REQUIRED to convert links)
CAPTION_TEMPLATE = os.environ.get("CAPTION_TEMPLATE", "{original_text}\\n\\n{footer}")
FOOTER_TEXT = os.environ.get("FOOTER_TEXT", "Shared via MyBrand")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", f"/webhook/{BOT_TOKEN[-20:] if BOT_TOKEN else 'secret'}")
SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")  # optional; recommended
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # if set, app will call setWebhook automatically
DB_PATH = os.environ.get("DB_PATH", "forwarder.sqlite3")
# -------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set. Exiting.")
if not DEST_CHANNELS:
    logger.error("DEST_CHANNELS is empty. Set as comma-separated list of destinations.")

DEST_LIST = [d.strip() for d in DEST_CHANNELS.split(",") if d.strip()]

app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# --------- sqlite dedupe ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS forwarded (
        source_chat_id TEXT,
        source_message_id INTEGER,
        forwarded_at INTEGER DEFAULT (strftime('%s','now')),
        PRIMARY KEY (source_chat_id, source_message_id)
    )
    """)
    conn.commit()
    conn.close()

def already_forwarded(chat_id: str, msg_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM forwarded WHERE source_chat_id=? AND source_message_id=?", (str(chat_id), int(msg_id)))
    rv = cur.fetchone() is not None
    conn.close()
    return rv

def mark_forwarded(chat_id: str, msg_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO forwarded(source_chat_id, source_message_id) VALUES (?,?)", (str(chat_id), int(msg_id)))
    conn.commit()
    conn.close()

# --------- Terabox detection & redirect ----------
TERABOX_RE_FRAGMENT = "terabox"

def is_terabox_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return TERABOX_RE_FRAGMENT in (parsed.netloc or "").lower() or TERABOX_RE_FRAGMENT in url.lower()
    except Exception:
        return False

def build_redirect(original_url: str) -> str:
    if not REDIRECT_BASE:
        return original_url
    encoded = quote_plus(original_url)
    return f"{REDIRECT_BASE.rstrip('/')}/?u={encoded}"

# ---------- entity-safe text rewrite & extract terabox links ----------
def replace_entities_in_text(text: str, entities: Optional[List[MessageEntity]]) -> str:
    if not entities:
        return text or ""
    pieces = []
    last = 0
    for ent in sorted(entities, key=lambda e: e.offset):
        s = ent.offset
        e = s + ent.length
        pieces.append(text[last:s])
        ent_text = text[s:e]
        if ent.type == "url":
            pieces.append(build_redirect(ent_text) if is_terabox_url(ent_text) else ent_text)
        elif ent.type == "text_link" and getattr(ent, "url", None):
            pieces.append(build_redirect(ent.url) if is_terabox_url(ent.url) else ent.url)
        else:
            pieces.append(ent_text)
        last = e
    pieces.append(text[last:])
    return "".join(pieces)

def extract_terabox_links_from_entities(text: str, entities: Optional[List[MessageEntity]]):
    links = []
    if not entities:
        return links
    for ent in entities:
        if ent.type == "url":
            s = ent.offset
            e = s + ent.length
            raw = text[s:e]
            if is_terabox_url(raw):
                links.append(raw)
        elif ent.type == "text_link" and getattr(ent, "url", None):
            if is_terabox_url(ent.url):
                links.append(ent.url)
    return links

def convert_inline_markup(reply_markup):
    if not reply_markup:
        return None
    rows = []
    for row in reply_markup.inline_keyboard:
        new_row = []
        for btn in row:
            if btn.url:
                new_url = build_redirect(btn.url) if is_terabox_url(btn.url) else btn.url
                new_row.append(InlineKeyboardButton(text=btn.text or "link", url=new_url))
            else:
                new_row.append(InlineKeyboardButton(text=btn.text or "btn", callback_data=btn.callback_data or "noop"))
        rows.append(new_row)
    return InlineKeyboardMarkup(rows)

def build_caption(original_text: str, src_channel: str, src_msg_id: int) -> str:
    subs = {
        "original_text": original_text or "",
        "source_channel": src_channel or "",
        "source_msg_id": str(src_msg_id),
        "footer": FOOTER_TEXT or ""
    }
    try:
        return CAPTION_TEMPLATE.format(**subs)
    except Exception as e:
        logger.exception("Caption template failed: %s", e)
        return (original_text or "") + "\\n\\n" + (FOOTER_TEXT or "")

# ---------- main handler ----------
async def handle_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    src_chat_id = msg.chat_id
    src_msg_id = msg.message_id

    if SOURCE_CHANNEL_ID and str(src_chat_id) != str(SOURCE_CHANNEL_ID):
        logger.debug("Ignored source %s (configured: %s)", src_chat_id, SOURCE_CHANNEL_ID)
        return

    if already_forwarded(src_chat_id, src_msg_id):
        logger.info("Already forwarded %s:%s", src_chat_id, src_msg_id)
        return

    logger.info("Processing %s:%s", src_chat_id, src_msg_id)

    original_text = ""
    entities = None
    if msg.text:
        original_text = msg.text
        entities = msg.entities or []
    elif msg.caption:
        original_text = msg.caption
        entities = msg.caption_entities or []

    new_text = replace_entities_in_text(original_text, entities)
    terabox_links = extract_terabox_links_from_entities(original_text, entities)

    inline_kb = convert_inline_markup(msg.reply_markup) if msg.reply_markup else None

    if terabox_links:
        tb_buttons = []
        seen = set()
        for l in terabox_links:
            if l in seen:
                continue
            seen.add(l)
            tb_buttons.append(InlineKeyboardButton(text="Open (Terabox)", url=build_redirect(l)))
        if inline_kb:
            rows = [list(r) for r in inline_kb.inline_keyboard]
            rows.append(tb_buttons)
            inline_kb = InlineKeyboardMarkup(rows)
        else:
            inline_kb = InlineKeyboardMarkup([tb_buttons])

    caption_to_send = build_caption(new_text, str(src_chat_id), src_msg_id)

    for dest in DEST_LIST:
        try:
            if msg.photo:
                await ctx.bot.send_photo(chat_id=dest, photo=msg.photo[-1].file_id, caption=caption_to_send or None, reply_markup=inline_kb)
            elif msg.document:
                await ctx.bot.send_document(chat_id=dest, document=msg.document.file_id, caption=caption_to_send or None, reply_markup=inline_kb)
            elif msg.video:
                await ctx.bot.send_video(chat_id=dest, video=msg.video.file_id, caption=caption_to_send or None, reply_markup=inline_kb)
            elif msg.audio:
                await ctx.bot.send_audio(chat_id=dest, audio=msg.audio.file_id, caption=caption_to_send or None, reply_markup=inline_kb)
            elif msg.voice:
                await ctx.bot.send_voice(chat_id=dest, voice=msg.voice.file_id, caption=caption_to_send or None, reply_markup=inline_kb)
            elif msg.sticker:
                await ctx.bot.send_sticker(chat_id=dest, sticker=msg.sticker.file_id)
                if caption_to_send:
                    await ctx.bot.send_message(chat_id=dest, text=caption_to_send)
            else:
                if caption_to_send:
                    await ctx.bot.send_message(chat_id=dest, text=caption_to_send, reply_markup=inline_kb)
                else:
                    await ctx.bot.forward_message(chat_id=dest, from_chat_id=src_chat_id, message_id=src_msg_id)
            logger.info("Posted to %s", dest)
        except Exception as e:
            logger.exception("Failed posting to %s: %s", dest, e)

    mark_forwarded(src_chat_id, src_msg_id)

# register handler
telegram_app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))

# ---------- FastAPI lifecycle ----------
@app.on_event("startup")
async def startup_event():
    logger.info("Initializing DB and Telegram app")
    init_db()
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Telegram app started")
    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
        logger.info("Setting webhook to %s", webhook_url)
        params = {"url": webhook_url}
        if SECRET_TOKEN:
            params["secret_token"] = SECRET_TOKEN
        try:
            resp = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook", data=params, timeout=10)
            j = resp.json()
            if not j.get("ok"):
                logger.error("setWebhook failed: %s", j)
            else:
                logger.info("setWebhook ok")
        except Exception as ex:
            logger.exception("setWebhook error: %s", ex)

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Telegram app")
    await telegram_app.stop()
    await telegram_app.shutdown()

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if SECRET_TOKEN:
        header_val = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header_val != SECRET_TOKEN:
            logger.warning("Invalid secret token header")
            raise HTTPException(status_code=403, detail="Invalid secret token")
    body = await request.json()
    update = Update.de_json(body, telegram_app.bot)
    await telegram_app.update_queue.put(update)
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok"}
