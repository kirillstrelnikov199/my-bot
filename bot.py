"""
Telegram Media Downloader Bot
Аналог @ttdlvbot — скачивает видео с TikTok, Instagram, YouTube, Twitter и др.

Зависимости:
    pip install python-telegram-bot yt-dlp aiohttp aiofiles

Запуск:
    BOT_TOKEN=7929718609:AAHx2F_jJ-6oXXFw4XUthLL3W8-Rcm0uAY0 python bot.py
"""

import os
import re
import asyncio
import logging
import tempfile
import aiofiles
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction

# ───────────────────────── Настройки ─────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "7929718609:AAHx2F_jJ-6oXXFw4XUthLL3W8-Rcm0uAY0")
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "tg_media_bot"
ADMIN_IDS: list[int] = []  # Опционально: список ID администраторов

# Обязательная подписка на канал
REQUIRED_CHANNEL = "@saverochekprod"
REQUIRED_CHANNEL_URL = "https://t.me/saverochekprod"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ───────────────────────── Проверка подписки ──────────────────────────────────

async def is_subscribed(user_id: int, bot) -> bool:
    """Возвращает True, если пользователь подписан на REQUIRED_CHANNEL."""
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logger.warning(f"is_subscribed check failed for user {user_id}: {e}")
        # Если бот не добавлен в канал как админ — пропускаем проверку
        return True


async def send_subscribe_prompt(chat_id: int, bot) -> None:
    """Отправляет сообщение с просьбой подписаться."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)],
        [InlineKeyboardButton("✅ Я подписался!", callback_data="check_sub")],
    ])
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "🔒 <b>Доступ ограничен</b>\n\n"
            f"Чтобы пользоваться ботом, подпишись на канал {REQUIRED_CHANNEL}\n\n"
            "После подписки нажми кнопку <b>«Я подписался!»</b> ниже."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )

# ───────────────────────── Вспомогательные функции ───────────────────────────

SUPPORTED_DOMAINS = [
    "tiktok.com", "vm.tiktok.com",
    "instagram.com", "www.instagram.com",
    "youtube.com", "youtu.be", "www.youtube.com",
    "twitter.com", "x.com",
    "vk.com",
    "reddit.com", "v.redd.it",
    "facebook.com", "fb.watch",
    "pinterest.com",
    "twitch.tv",
    "soundcloud.com",
    "bilibili.com",
]

def extract_url(text: str) -> str | None:
    """Извлекает первую URL из текста."""
    pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    match = re.search(pattern, text)
    return match.group(0) if match else None

def is_supported(url: str) -> bool:
    """Проверяет, поддерживается ли домен."""
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)
    except Exception:
        return False

def human_size(bytes_: int) -> str:
    for unit in ["Б", "КБ", "МБ", "ГБ"]:
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} ТБ"

def platform_emoji(url: str) -> str:
    url_lower = url.lower()
    if "tiktok" in url_lower:    return "🎵"
    if "instagram" in url_lower: return "📸"
    if "youtube" in url_lower or "youtu.be" in url_lower: return "▶️"
    if "twitter" in url_lower or "x.com" in url_lower:   return "🐦"
    if "vk.com" in url_lower:    return "💙"
    if "reddit" in url_lower:    return "🤖"
    if "facebook" in url_lower or "fb.watch" in url_lower: return "👥"
    if "twitch" in url_lower:    return "🟣"
    if "soundcloud" in url_lower: return "🔊"
    return "🎬"

# ───────────────────────── Загрузка медиа через yt-dlp ───────────────────────

async def get_info(url: str) -> dict | None:
    """Получает метаданные видео без загрузки."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    loop = asyncio.get_event_loop()
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        return info
    except Exception as e:
        logger.warning(f"get_info failed for {url}: {e}")
        return None


async def download_media(url: str, audio_only: bool = False, quality: str = "best") -> tuple[Path | None, dict]:
    """
    Загружает медиа и возвращает (путь_к_файлу, метаданные).
    quality: "best" | "720" | "480" | "360"
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if audio_only:
        fmt = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        if quality == "best":
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        else:
            fmt = (
                f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]"
                f"/best[height<={quality}][ext=mp4]/best[height<={quality}]/best"
            )
        postprocessors = []

    ydl_opts = {
        "format": fmt,
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "postprocessors": postprocessors,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
    }

    loop = asyncio.get_event_loop()
    meta: dict = {}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(
                None, lambda: ydl.extract_info(url, download=True)
            )
            if info:
                meta = {
                    "title": info.get("title", ""),
                    "uploader": info.get("uploader") or info.get("channel", ""),
                    "duration": info.get("duration"),
                    "view_count": info.get("view_count"),
                    "like_count": info.get("like_count"),
                    "ext": info.get("ext", "mp4"),
                    "id": info.get("id", ""),
                    "thumbnail": info.get("thumbnail"),
                }
                candidates = list(DOWNLOAD_DIR.glob(f"{meta['id']}.*"))
                if candidates:
                    return candidates[0], meta
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"DownloadError: {e}")
    except Exception as e:
        logger.error(f"Unexpected download error: {e}")

    return None, meta

# ───────────────────────── Тексты ────────────────────────────────────────────

WELCOME_TEXT = """
👋 <b>Привет! Я медиа-загрузчик.</b>

Просто отправь мне ссылку на видео — я скачаю его для тебя без водяного знака!

<b>Поддерживаемые платформы:</b>
🎵 TikTok • 📸 Instagram • ▶️ YouTube
🐦 Twitter/X • 💙 VK • 🤖 Reddit
👥 Facebook • 🟣 Twitch • 🔊 SoundCloud
и <b>1000+</b> других сайтов!

<b>Команды:</b>
/start — это сообщение
/help — помощь и примеры
/quality — выбрать качество по умолчанию

<i>Поддержка: просто отправь ссылку в чат!</i>
"""

HELP_TEXT = """
<b>📖 Как пользоваться:</b>

1. Скопируй ссылку на видео (TikTok, Instagram, YouTube и т.д.)
2. Отправь её боту
3. Выбери формат: видео или аудио (MP3)
4. Получи файл!

<b>Примеры ссылок:</b>
• <code>https://www.tiktok.com/@user/video/123456</code>
• <code>https://www.instagram.com/reel/ABC123/</code>
• <code>https://youtu.be/dQw4w9WgXcQ</code>
• <code>https://twitter.com/user/status/123456</code>

<b>⚠️ Ограничения:</b>
• Приватные видео не поддерживаются
• YouTube Shorts требует полную ссылку
"""

# ───────────────────────── Хэндлеры команд ───────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context.bot):
        await send_subscribe_prompt(update.effective_chat.id, context.bot)
        return
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context.bot):
        await send_subscribe_prompt(update.effective_chat.id, context.bot)
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(user_id, context.bot):
        await send_subscribe_prompt(update.effective_chat.id, context.bot)
        return
    current = context.user_data.get("quality", "best")
    keyboard = [
        [
            InlineKeyboardButton("🏆 Лучшее", callback_data="set_q:best"),
            InlineKeyboardButton("📺 720p", callback_data="set_q:720"),
        ],
        [
            InlineKeyboardButton("📱 480p", callback_data="set_q:480"),
            InlineKeyboardButton("🔋 360p", callback_data="set_q:360"),
        ],
    ]
    labels = {"best": "Лучшее", "720": "720p", "480": "480p", "360": "360p"}
    await update.message.reply_text(
        f"⚙️ <b>Качество видео</b>\n\nТекущее: <b>{labels.get(current, current)}</b>\n\nВыбери желаемое:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cb_set_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    q = query.data.split(":")[1]
    context.user_data["quality"] = q
    labels = {"best": "Лучшее (Best)", "720": "720p HD", "480": "480p", "360": "360p"}
    await query.edit_message_text(
        f"✅ Качество установлено: <b>{labels.get(q, q)}</b>",
        parse_mode=ParseMode.HTML,
    )

# ───────────────────────── Callback: проверка подписки ───────────────────────

async def cb_check_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if await is_subscribed(user_id, context.bot):
        await query.answer("✅ Подписка подтверждена!", show_alert=False)
        await query.edit_message_text(
            "✅ <b>Отлично! Подписка подтверждена.</b>\n\nТеперь отправь мне ссылку на видео!",
            parse_mode=ParseMode.HTML,
        )
    else:
        await query.answer("❌ Ты ещё не подписался!", show_alert=True)

# ───────────────────────── Основной хэндлер сообщений ────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Проверка подписки перед любым действием
    if not await is_subscribed(user_id, context.bot):
        await send_subscribe_prompt(update.effective_chat.id, context.bot)
        return

    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text(
            "🔗 Отправь мне ссылку на видео!\n\n"
            "Используй /help чтобы увидеть список поддерживаемых платформ."
        )
        return

    emoji = platform_emoji(url)
    keyboard = [
        [
            InlineKeyboardButton("🎬 Видео", callback_data=f"dl:video:{url}"),
            InlineKeyboardButton("🎵 Аудио (MP3)", callback_data=f"dl:audio:{url}"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ]
    await update.message.reply_text(
        f"{emoji} <b>Ссылка получена!</b>\n\n<code>{url[:80]}{'...' if len(url) > 80 else ''}</code>\n\nВыбери формат:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def cb_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Отменено.")
        return

    # Проверка подписки при нажатии кнопки скачивания
    if not await is_subscribed(query.from_user.id, context.bot):
        await query.edit_message_text(
            f"🔒 Для использования бота подпишись на {REQUIRED_CHANNEL}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Подписаться", url=REQUIRED_CHANNEL_URL)],
                [InlineKeyboardButton("✅ Я подписался!", callback_data="check_sub")],
            ]),
        )
        return

    parts = query.data.split(":", 2)
    if len(parts) < 3:
        await query.edit_message_text("❌ Ошибка: неверный формат данных.")
        return

    _, mode, url = parts
    audio_only = (mode == "audio")
    quality = context.user_data.get("quality", "best")

    emoji = platform_emoji(url)
    await query.edit_message_text(
        f"{emoji} <b>Загружаю{'  аудио' if audio_only else ' видео'}...</b>\n\n"
        f"⏳ Пожалуйста, подожди — это может занять несколько секунд.",
        parse_mode=ParseMode.HTML,
    )

    await context.bot.send_chat_action(
        chat_id=query.message.chat_id,
        action=ChatAction.UPLOAD_VIDEO if not audio_only else ChatAction.UPLOAD_VOICE,
    )

    file_path, meta = await download_media(url, audio_only=audio_only, quality=quality)

    if not file_path or not file_path.exists():
        await query.edit_message_text(
            "❌ <b>Не удалось скачать видео.</b>\n\n"
            "Возможные причины:\n"
            "• Приватное видео\n"
            "• Ссылка недействительна\n"
            "• Платформа заблокировала загрузку\n\n"
            "Попробуй другую ссылку или платформу.",
            parse_mode=ParseMode.HTML,
        )
        return

    file_size = file_path.stat().st_size

    # Формируем подпись
    caption_lines = []
    if meta.get("title"):
        caption_lines.append(f"<b>{meta['title'][:100]}</b>")
    if meta.get("uploader"):
        caption_lines.append(f"👤 {meta['uploader']}")
    if meta.get("duration"):
        d = int(meta["duration"])
        caption_lines.append(f"⏱ {d // 60}:{d % 60:02d}")
    if meta.get("view_count"):
        caption_lines.append(f"👁 {meta['view_count']:,}")
    caption_lines.append(f"\n📦 {human_size(file_size)}")
    caption = "\n".join(caption_lines)

    try:
        await query.edit_message_text(f"{emoji} Отправляю файл...", parse_mode=ParseMode.HTML)

        async with aiofiles.open(file_path, "rb") as f:
            data = await f.read()

        if audio_only:
            await context.bot.send_audio(
                chat_id=query.message.chat_id,
                audio=data,
                caption=caption,
                parse_mode=ParseMode.HTML,
                filename=file_path.name,
            )
        else:
            await context.bot.send_video(
                chat_id=query.message.chat_id,
                video=data,
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
            )

        await query.delete_message()
        logger.info(f"Sent {file_path.name} ({human_size(file_size)}) to user {query.from_user.id}")

    except Exception as e:
        logger.error(f"Send error: {e}")
        await query.edit_message_text(
            "❌ Ошибка при отправке файла. Попробуй ещё раз.",
            parse_mode=ParseMode.HTML,
        )
    finally:
        file_path.unlink(missing_ok=True)


# ───────────────────────── Запуск бота ───────────────────────────────────────

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Начать работу"),
        BotCommand("help", "Помощь и примеры"),
        BotCommand("quality", "Выбрать качество видео"),
    ])
    logger.info("Bot commands registered.")


def main():
    if BOT_TOKEN == "ВСТАВЬТЕ_ВАШ_ТОКЕН_СЮДА":
        print("❌ Укажи токен бота!\n"
              "   Получи его у @BotFather и установи переменную окружения:\n"
              "   export BOT_TOKEN=123456:ABC-DEF...\n"
              "   или замени значение BOT_TOKEN в начале файла bot.py")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("quality", cmd_quality))
    app.add_handler(CallbackQueryHandler(cb_check_sub, pattern=r"^check_sub$"))
    app.add_handler(CallbackQueryHandler(cb_set_quality, pattern=r"^set_q:"))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^(dl:|cancel)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
