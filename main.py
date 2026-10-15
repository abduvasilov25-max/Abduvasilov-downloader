import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from html import unescape
from pathlib import Path

import aiohttp
import aiosqlite
import yt_dlp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

# ====================== SOZLAMALAR ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8844213733:AAEaEjjQg7FnLrRGpc456XO1SJeQCYDOQPw")
DB_PATH = "bot.db"
COOKIES_FILE = "cookies.txt"
MAX_SIZE = 50 * 1024 * 1024           # Telegram bot limiti: 50 MB
MAX_PARALLEL = 6
# ========================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

router = Router()
SEM = asyncio.Semaphore(MAX_PARALLEL)
http: aiohttp.ClientSession | None = None
BOT_USERNAME = ""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
URL_RE = re.compile(r"https?://[^\s<>\"']+")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT = {".mp4", ".mkv", ".webm", ".mov"}

# ====================== MATNLAR ======================
TEXTS = {
    "uz": {
        "choose": "🌐 Tilni tanlang / Выберите язык / Choose language",
        "welcome": (
            "👋 Salom! Men video yuklovchi botman.\n\n"
            "📎 TikTok, Instagram, Pinterest yoki YouTube havolasini yuboring — "
            "men videoni tez yuklab beraman.\n\n"
            "🌐 Tilni o'zgartirish: /lang"
        ),
        "wait": "⏳ Yuklanmoqda...",
        "no_link": "❌ Havola topilmadi. TikTok, Instagram, Pinterest yoki YouTube havolasini yuboring.",
        "too_big": "⚠️ Video juda katta (50 MB dan oshadi). Telegram bot cheklovi tufayli yuborib bo'lmaydi.",
        "error": "❌ Yuklab bo'lmadi. Havola to'g'riligini tekshiring yoki keyinroq urinib ko'ring.",
    },
    "ru": {
        "choose": "🌐 Tilni tanlang / Выберите язык / Choose language",
        "welcome": (
            "👋 Привет! Я бот для скачивания видео.\n\n"
            "📎 Отправьте ссылку на TikTok, Instagram, Pinterest или YouTube — "
            "я быстро скачаю видео.\n\n"
            "🌐 Сменить язык: /lang"
        ),
        "wait": "⏳ Загрузка...",
        "no_link": "❌ Ссылка не найдена. Отправьте ссылку на TikTok, Instagram, Pinterest или YouTube.",
        "too_big": "⚠️ Видео слишком большое (больше 50 МБ). Telegram не позволяет боту отправить его.",
        "error": "❌ Не удалось скачать. Проверьте ссылку или попробуйте позже.",
    },
    "en": {
        "choose": "🌐 Tilni tanlang / Выберите язык / Choose language",
        "welcome": (
            "👋 Hi! I'm a video downloader bot.\n\n"
            "📎 Send a TikTok, Instagram, Pinterest or YouTube link — "
            "I'll download the video fast.\n\n"
            "🌐 Change language: /lang"
        ),
        "wait": "⏳ Downloading...",
        "no_link": "❌ No link found. Send a TikTok, Instagram, Pinterest or YouTube link.",
        "too_big": "⚠️ The video is too large (over 50 MB). Telegram doesn't allow bots to send it.",
        "error": "❌ Download failed. Check the link or try again later.",
    },
}


def tr(lang: str, key: str) -> str:
    return TEXTS.get(lang, TEXTS["en"])[key]


def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🇺🇿 UZ", callback_data="lang:uz"),
            InlineKeyboardButton(text="🇷🇺 RU", callback_data="lang:ru"),
            InlineKeyboardButton(text="🇬🇧 ENG", callback_data="lang:en"),
        ]]
    )


# ====================== BAZA ======================
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, lang TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS cache(url TEXT PRIMARY KEY, file_id TEXT, kind TEXT)")
        await db.commit()


async def get_lang(uid: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT lang FROM users WHERE user_id=?", (uid,)) as c:
            row = await c.fetchone()
    return row[0] if row else None


async def set_lang(uid: int, lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users(user_id, lang) VALUES(?, ?)", (uid, lang))
        await db.commit()


async def cache_get(url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT file_id, kind FROM cache WHERE url=?", (url,)) as c:
            return await c.fetchone()


async def cache_set(url: str, file_id: str, kind: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO cache(url, file_id, kind) VALUES(?,?,?)", (url, file_id, kind))
        await db.commit()


# ====================== YUKLASH ======================
class TooBig(Exception):
    pass


def platform(url: str) -> str | None:
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    if "pinterest." in u or "pin.it" in u:
        return "pinterest"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return None


async def download_file(url: str, path: Path):
    async with http.get(url, headers=HEADERS, allow_redirects=True) as r:
        r.raise_for_status()
        size = 0
        with open(path, "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 16):
                size += len(chunk)
                if size > MAX_SIZE:
                    raise TooBig()
                f.write(chunk)


def ytdlp_download(url: str, out_dir: str):
    opts = {
        "outtmpl": f"{out_dir}/%(id)s.%(ext)s",
        "format": "best",  # Ffmpeg talab qilmaydigan eng ishonchli format
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 25,
        "http_headers": HEADERS,
    }
    if Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    files = [
        f for f in Path(out_dir).iterdir()
        if f.suffix.lower() in IMG_EXT | VID_EXT
    ]
    if not files:
        raise RuntimeError("fayl topilmadi")
    f = max(files, key=lambda x: x.stat().st_size)
    if f.stat().st_size > MAX_SIZE:
        raise TooBig()
    kind = "photo" if f.suffix.lower() in IMG_EXT else "video"
    return [(f, kind)]


async def tiktok_api(url: str, workdir: str):
    async with http.post(
        "https://www.tikwm.com/api/",
        data={"url": url},
        headers=HEADERS,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        j = await r.json(content_type=None)
    d = j.get("data")
    if j.get("code") != 0 or not d:
        raise RuntimeError("tikwm xato")

    if d.get("images"):
        paths = [Path(workdir) / f"{i}.jpg" for i in range(len(d["images"][:10]))]
        await asyncio.gather(*(download_file(u, p) for u, p in zip(d["images"], paths)))
        return [(p, "photo") for p in paths]

    v = d.get("play") or d.get("hdplay")
    if not v:
        raise RuntimeError("video yo'q")
    if v.startswith("/"):
        v = "https://www.tikwm.com" + v
    return [(v, "video")]


async def pinterest_api(url: str, workdir: str):
    """Pinterest uchun yuz foiz ishlaydigan maxsus parser."""
    async with http.get(url, headers=HEADERS, allow_redirects=True) as r:
        html = await r.text()

    # Video havolasini qidirish
    mp4_matches = re.findall(r'"contentUrl"\s*:\s*"(https://[^"]+?\.mp4[^"]*)"', html) or \
                  re.findall(r'<meta[^>]+property="og:video(?::url)?"[^>]+content="([^"]+\.mp4[^"]*)"', html) or \
                  re.findall(r'(https://v1\.pinimg\.com/videos/[^\s"\'\\]+\.mp4)', html) or \
                  re.findall(r'(https://i\.pinimg\.com/videos/[^\s"\'\\]+\.mp4)', html)

    if mp4_matches:
        v_url = unescape(mp4_matches[0]).replace("\\/", "/")
        p = Path(workdir) / "pinterest.mp4"
        await download_file(v_url, p)
        return [(p, "video")]

    # Agar video bo'lmasa, rasmni qidirish
    img_matches = re.findall(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
    if img_matches:
        img_url = unescape(img_matches[0])
        img_url = re.sub(r"/\d+x/", "/originals/", img_url)
        p = Path(workdir) / "pinterest.jpg"
        try:
            await download_file(img_url, p)
        except Exception:
            img_url = re.sub(r"/originals/", "/736x/", img_url)
            await download_file(img_url, p)
        return [(p, "photo")]

    raise RuntimeError("Pinterest media topilmadi")


async def fetch_media(url: str, workdir: str):
    p = platform(url)

    if p == "tiktok":
        try:
            return await tiktok_api(url, workdir)
        except TooBig:
            raise
        except Exception as e:
            log.warning("tikwm ishlamadi: %s", e)

    if p == "pinterest":
        try:
            return await pinterest_api(url, workdir)
        except TooBig:
            raise
        except Exception as e:
            log.warning("Pinterest maxsus yuklashda xatolik: %s", e)

    try:
        return await asyncio.to_thread(ytdlp_download, url, workdir)
    except TooBig:
        raise
    except Exception as e:
        log.warning("yt-dlp xato (%s): %s", p, e)
        raise


# ====================== YUBORISH ======================
async def send_one(message: Message, media, kind: str, caption: str | None) -> str:
    if kind == "video":
        m = await message.answer_video(media, caption=caption, supports_streaming=True)
        return m.video.file_id
    m = await message.answer_photo(media, caption=caption)
    return m.photo[-1].file_id


async def send_result(message: Message, items, url: str, workdir: str):
    caption = f"📥 @{BOT_USERNAME}" if BOT_USERNAME else None

    if len(items) == 1:
        src, kind = items[0]
        if isinstance(src, str):
            try:
                file_id = await send_one(message, src, kind, caption)
            except Exception as e:
                log.info("URL orqali bo'lmadi (%s) -> fayl sifatida yuklanmoqda", e)
                p = Path(workdir) / ("fallback.mp4" if kind == "video" else "fallback.jpg")
                await download_file(src, p)
                file_id = await send_one(message, FSInputFile(p), kind, caption)
        else:
            file_id = await send_one(message, FSInputFile(src), kind, caption)
        await cache_set(url, file_id, kind)
        return

    media = [
        InputMediaPhoto(media=FSInputFile(p), caption=caption if i == 0 else None)
        for i, (p, _) in enumerate(items)
    ]
    await message.answer_media_group(media)


# ====================== HANDLERLAR ======================
@router.message(CommandStart())
@router.message(Command("lang"))
async def cmd_start(message: Message):
    await message.answer(TEXTS["uz"]["choose"], reply_markup=lang_kb())


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery):
    lang = call.data.split(":")[1]
    await set_lang(call.from_user.id, lang)
    await call.message.edit_text(tr(lang, "welcome"))
    await call.answer()


@router.message(F.text)
async def on_text(message: Message):
    uid = message.from_user.id
    lang = await get_lang(uid)
    if not lang:
        await message.answer(TEXTS["uz"]["choose"], reply_markup=lang_kb())
        return

    urls = [u.rstrip(").,") for u in URL_RE.findall(message.text) if platform(u)]
    if not urls:
        await message.answer(tr(lang, "no_link"))
        return
    url = urls[0]

    cached = await cache_get(url)
    if cached:
        file_id, kind = cached
        caption = f"📥 @{BOT_USERNAME}" if BOT_USERNAME else None
        try:
            if kind == "video":
                await message.answer_video(file_id, caption=caption)
            else:
                await message.answer_photo(file_id, caption=caption)
            return
        except Exception:
            pass

    status = await message.answer(tr(lang, "wait"))
    await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
    workdir = tempfile.mkdtemp(prefix="dl_")
    try:
        t0 = time.perf_counter()
        async with SEM:
            items = await fetch_media(url, workdir)
        t1 = time.perf_counter()
        await send_result(message, items, url, workdir)
        t2 = time.perf_counter()
        log.info("VAQT | olish: %.1fs | yuborish: %.1fs | %s", t1 - t0, t2 - t1, url)
    except TooBig:
        await message.answer(tr(lang, "too_big"))
    except Exception:
        log.exception("yuklashda xato: %s", url)
        await message.answer(tr(lang, "error"))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            await status.delete()
        except Exception:
            pass


# ====================== ISHGA TUSHIRISH ======================
async def main():
    global http, BOT_USERNAME
    await db_init()
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
    bot = Bot(
        BOT_TOKEN,
        session=AiohttpSession(timeout=300),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    BOT_USERNAME = (await bot.get_me()).username or ""
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await http.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())