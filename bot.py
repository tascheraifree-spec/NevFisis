import asyncio
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, InputSticker, Message

load_dotenv()
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

MAX_STICKERS_PER_SET = 200
MAX_WEBM_BYTES = 256 * 1024
router = Router()
locks: dict[int, asyncio.Lock] = {}
bot_username = ""


def user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in locks:
        locks[user_id] = asyncio.Lock()
    return locks[user_id]


def run_ffmpeg(source: Path, target: Path, is_image: bool) -> None:
    # Telegram video stickers: VP9 WEBM, <=3 seconds, no audio,
    # one side exactly 512 px, <=256 KiB.
    attempts = [(38, 30), (44, 30), (49, 24), (54, 20), (59, 15), (63, 10)]
    vf = (
        "scale=512:512:force_original_aspect_ratio=decrease,"
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0,"
        "setsar=1,format=yuva420p"
    )

    for crf, fps in attempts:
        target.unlink(missing_ok=True)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if is_image:
            cmd += ["-loop", "1", "-i", str(source), "-t", "2.9"]
        else:
            cmd += ["-i", str(source), "-t", "2.9"]
        cmd += [
            "-vf", f"{vf},fps={fps}",
            "-an",
            "-c:v", "libvpx-vp9",
            "-b:v", "0",
            "-crf", str(crf),
            "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0",
            "-row-mt", "1",
            "-deadline", "good",
            "-cpu-used", "4",
            str(target),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if result.returncode == 0 and target.exists() and 0 < target.stat().st_size <= MAX_WEBM_BYTES:
            return

    raise RuntimeError("Не удалось ужать файл до лимита Telegram (256 КБ)")


def pack_name(user_id: int, number: int) -> str:
    suffix = "" if number == 1 else f"_{number}"
    return f"mix_{user_id}{suffix}_by_{bot_username.lower()}"


def pack_title(message: Message, number: int) -> str:
    owner = message.from_user.first_name if message.from_user else "Мои"
    suffix = "" if number == 1 else f" #{number}"
    return f"Стикеры {owner}{suffix}"[:64]


async def find_pack(bot: Bot, user_id: int) -> tuple[str, bool, int]:
    """Return (name, exists, number), moving to the next pack when full."""
    for number in range(1, 100):
        name = pack_name(user_id, number)
        try:
            sticker_set = await bot.get_sticker_set(name)
            if len(sticker_set.stickers) < MAX_STICKERS_PER_SET:
                return name, True, number
        except TelegramBadRequest as exc:
            if "STICKERSET_INVALID" in str(exc).upper():
                return name, False, number
            raise
    raise RuntimeError("Слишком много наборов")


def media_from_message(message: Message):
    if message.photo:
        return message.photo[-1], True, ".jpg"
    if message.animation:
        suffix = Path(message.animation.file_name or "file.gif").suffix or ".gif"
        return message.animation, False, suffix
    if message.video:
        suffix = Path(message.video.file_name or "file.mp4").suffix or ".mp4"
        return message.video, False, suffix
    if message.document:
        mime = message.document.mime_type or ""
        if mime == "image/gif":
            return message.document, False, ".gif"
        if mime.startswith("image/"):
            suffix = Path(message.document.file_name or "image").suffix
            suffix = suffix or mimetypes.guess_extension(mime) or ".png"
            return message.document, True, suffix
        if mime.startswith("video/"):
            suffix = Path(message.document.file_name or "video.mp4").suffix or ".mp4"
            return message.document, False, suffix
    return None


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Пришли фото, GIF или короткое видео — я превращу его в видео-стикер "
        "и добавлю в твой набор. Фото останется неподвижным, но сможет лежать "
        "в одном наборе с анимацией."
    )


@router.message(F.photo | F.animation | F.video | F.document)
async def make_sticker(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    media = media_from_message(message)
    if not media:
        await message.answer("Поддерживаются фото, GIF и видеофайлы.")
        return

    tg_file, is_image, suffix = media
    status = await message.answer("Делаю стикер…")
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)

    async with user_lock(message.from_user.id):
        temp_dir = Path(tempfile.mkdtemp(prefix="sticker_"))
        try:
            source = temp_dir / f"source{suffix}"
            target = temp_dir / "sticker.webm"
            remote_file = await bot.get_file(tg_file.file_id)
            await bot.download_file(remote_file.file_path, destination=source)
            await asyncio.to_thread(run_ffmpeg, source, target, is_image)

            sticker = InputSticker(
                sticker=FSInputFile(target),
                format="video",
                emoji_list=["✨"],
            )
            name, exists, number = await find_pack(bot, message.from_user.id)
            if exists:
                await bot.add_sticker_to_set(
                    user_id=message.from_user.id,
                    name=name,
                    sticker=sticker,
                )
            else:
                await bot.create_new_sticker_set(
                    user_id=message.from_user.id,
                    name=name,
                    title=pack_title(message, number),
                    stickers=[sticker],
                    sticker_type="regular",
                )

            await status.edit_text(
                f"Готово ✨\nhttps://t.me/addstickers/{name}",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as exc:
            logging.exception("Telegram rejected sticker")
            text = str(exc)
            if "USER_IS_BOT" in text.upper():
                text = "Набор можно создать только для обычного пользователя."
            await status.edit_text(f"Telegram не принял стикер: {text}")
        except Exception as exc:
            logging.exception("Sticker processing failed")
            await status.edit_text(f"Не получилось обработать файл: {exc}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


@router.message()
async def unsupported(message: Message) -> None:
    await message.answer("Пришли фото, GIF или короткое видео.")


async def main() -> None:
    global bot_username
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN)
    me = await bot.get_me()
    bot_username = me.username or "bot"
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
