import os
import re
import shutil
import logging
import asyncio
import tempfile
import subprocess
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import yt_dlp

# ─── Настройки ───────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")

LIMIT_VIDEO_MB = 50
LIMIT_DOC_MB   = 2000

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INSTAGRAM_PATTERN = re.compile(
    r"https?://(www\.)?instagram\.com/(p|reel|tv)/[\w-]+/?(\?.*)?",
    re.IGNORECASE,
)

# ─── ffmpeg ───────────────────────────────────────────────────────────────────

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def compress_video(input_path: Path, output_path: Path, target_mb: int = 49) -> bool:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(input_path),
        ],
        capture_output=True, text=True,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError:
        logger.error("ffprobe не смог определить длительность")
        return False

    target_bits = target_mb * 8 * 1024
    audio_kbps  = 128
    video_kbps  = int(target_bits / duration) - audio_kbps
    if video_kbps < 100:
        logger.warning("Слишком низкий целевой битрейт: %d kbps", video_kbps)
        return False

    tmp_log = str(output_path.parent / "ffmpeg2pass")

    pass1 = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(input_path),
            "-c:v", "libx264", "-b:v", f"{video_kbps}k",
            "-pass", "1", "-passlogfile", tmp_log,
            "-an", "-f", "null", "/dev/null",
        ],
        capture_output=True,
    )
    if pass1.returncode != 0:
        logger.error("ffmpeg pass1 error: %s", pass1.stderr.decode())
        return False

    pass2 = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(input_path),
            "-c:v", "libx264", "-b:v", f"{video_kbps}k",
            "-pass", "2", "-passlogfile", tmp_log,
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            str(output_path),
        ],
        capture_output=True,
    )
    if pass2.returncode != 0:
        logger.error("ffmpeg pass2 error: %s", pass2.stderr.decode())
        return False

    return True


# ─── Загрузка ─────────────────────────────────────────────────────────────────

def _download_sync(url: str, ydl_opts: dict) -> dict | None:
    """Синхронная загрузка — запускается через asyncio.to_thread()"""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)


async def download_video(url: str, tmp_dir: str) -> tuple[Path | None, dict | None]:
    output_path = os.path.join(tmp_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": output_path,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    # ✅ asyncio.to_thread вместо get_event_loop().run_in_executor — работает на Python 3.14
    info = await asyncio.to_thread(_download_sync, url, ydl_opts)

    files = list(Path(tmp_dir).glob("*.mp4"))
    if not files:
        files = list(Path(tmp_dir).glob("*.*"))
    if not files:
        return None, info
    return files[0], info


# ─── Хендлеры ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ffmpeg_status = "✅ установлен" if ffmpeg_available() else "⚠️ не найден (сжатие недоступно)"
    await update.message.reply_text(
        "👋 Привет! Отправь мне ссылку на пост, Reel или IGTV из Instagram.\n\n"
        "Поддерживаемые форматы:\n"
        "• instagram.com/p/...\n"
        "• instagram.com/reel/...\n"
        "• instagram.com/tv/...\n\n"
        f"🎬 ffmpeg: {ffmpeg_status}\n"
        f"📦 Лимит как видео: {LIMIT_VIDEO_MB} MB\n"
        f"📁 Лимит как файл: {LIMIT_DOC_MB} MB"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ℹ️ Как работает бот:\n\n"
        f"1. Скачивает видео из Instagram\n"
        f"2. Если < {LIMIT_VIDEO_MB} MB — отправляет как видео\n"
        f"3. Если > {LIMIT_VIDEO_MB} MB и есть ffmpeg — сжимает\n"
        f"4. Если сжатие не помогло — отправляет как файл (до {LIMIT_DOC_MB} MB)\n\n"
        "Если видео не скачивается:\n"
        "• Аккаунт закрытый (приватный)\n"
        "• Ссылка неверная\n"
        "• Instagram временно заблокировал запрос"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    url_match = INSTAGRAM_PATTERN.search(text)

    if not url_match:
        await update.message.reply_text(
            "❌ Не нашёл ссылку на Instagram. "
            "Убедись, что ссылка содержит instagram.com/p/, /reel/ или /tv/"
        )
        return

    url = url_match.group(0)
    status_msg = await update.message.reply_text("⏳ Скачиваю видео...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            video_file, info = await download_video(url, tmp_dir)

            if video_file is None or not video_file.exists():
                await status_msg.edit_text("❌ Не удалось скачать видео. Попробуй позже.")
                return

            file_size_mb = video_file.stat().st_size / (1024 * 1024)
            caption = (info or {}).get("title", "") or ""
            if len(caption) > 1024:
                caption = caption[:1021] + "..."

            # Случай 1: файл маленький — сразу как видео
            if file_size_mb <= LIMIT_VIDEO_MB:
                await status_msg.edit_text("📤 Отправляю видео...")
                await send_as_video(update, video_file, caption)
                await status_msg.delete()
                return

            # Случай 2: большой — пробуем сжать через ffmpeg
            if ffmpeg_available():
                await status_msg.edit_text(
                    f"📦 Видео {file_size_mb:.0f} MB — сжимаю через ffmpeg..."
                )
                compressed = Path(tmp_dir) / "compressed.mp4"
                success = await asyncio.to_thread(compress_video, video_file, compressed)

                if success and compressed.exists():
                    compressed_mb = compressed.stat().st_size / (1024 * 1024)
                    logger.info("Сжато: %.1f MB → %.1f MB", file_size_mb, compressed_mb)

                    if compressed_mb <= LIMIT_VIDEO_MB:
                        await status_msg.edit_text(
                            f"✅ Сжато до {compressed_mb:.1f} MB. Отправляю..."
                        )
                        await send_as_video(update, compressed, caption)
                        await status_msg.delete()
                        return
                    else:
                        video_file = compressed
                        file_size_mb = compressed_mb
                else:
                    logger.warning("ffmpeg не смог сжать файл")

            # Случай 3: отправляем как документ
            if file_size_mb > LIMIT_DOC_MB:
                await status_msg.edit_text(
                    f"❌ Видео слишком большое ({file_size_mb:.0f} MB). Даже как файл не отправить."
                )
                return

            await status_msg.edit_text(
                f"📁 Видео {file_size_mb:.0f} MB — отправляю как файл\n"
                "(воспроизводится после скачивания)"
            )
            await send_as_document(update, video_file, caption)
            await status_msg.delete()

        except yt_dlp.utils.DownloadError as e:
            logger.error("DownloadError: %s", e)
            msg = str(e)
            if "Private" in msg or "login" in msg.lower():
                await status_msg.edit_text(
                    "🔒 Это видео из приватного аккаунта. Скачать не получится."
                )
            else:
                await status_msg.edit_text(
                    f"❌ Ошибка при скачивании:\n<code>{str(e)[:300]}</code>",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.exception("Unexpected error")
            await status_msg.edit_text(f"❌ Неожиданная ошибка: {str(e)[:200]}")


async def send_as_video(update: Update, path: Path, caption: str) -> None:
    with open(path, "rb") as f:
        await update.message.reply_video(
            video=f,
            caption=caption or None,
            supports_streaming=True,
        )


async def send_as_document(update: Update, path: Path, caption: str) -> None:
    with open(path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=path.name,
            caption=caption or None,
        )


# ─── Запуск ──────────────────────────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        raise ValueError(
            "Укажи токен! Либо переменная окружения BOT_TOKEN, либо прямо в коде."
        )

    if not ffmpeg_available():
        logger.warning("ffmpeg не найден! sudo apt install ffmpeg")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен. Нажми Ctrl+C для остановки.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
