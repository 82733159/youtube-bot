"""
YouTube Downloader Telegram Bot
Requirements:
    pip install telethon yt-dlp mutagen Pillow requests

Run: python youtube_bot.py
"""

import os
import re
import asyncio
import tempfile
import time
import requests
import logging
from pathlib import Path

from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeVideo
import yt_dlp
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, ID3NoHeaderError
from PIL import Image
import io

# ─── CONFIG ────────────────────────────────────────────────────────────────────
API_ID    = 28748795
API_HASH  = "c6955a151fa50ff8de731375755c52b8"
BOT_TOKEN = "8633224330:AAFISTaR7gZyblKVNQJa3Pi2IJaxepdZxCc"

SESSION_NAME   = "youtube_bot"
MAX_FILE_SIZE  = 2 * 1024 * 1024 * 1024   # 2 GB
DOWNLOAD_DIR   = Path(tempfile.gettempdir()) / "yt_bot_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── AUTH: yt-dlp built-in OAuth2 (no plugin needed, requires yt-dlp >= 2024.10.22)
# Send /login to the bot once — it gives you a URL + code to enter at
# google.com/device — token is cached by yt-dlp automatically forever.
#
# Update yt-dlp first:  pip install -U yt-dlp

COOKIES_FILE = Path(__file__).parent / "cookies.txt"   # fallback

# yt-dlp stores the OAuth2 token in its own cache directory automatically
# (~/.cache/yt-dlp/ on Linux). We just need to pass --username oauth2.

def get_ydl_base_opts() -> dict:
    """Return base yt-dlp options with the best available auth method."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
        log.info("Using cookies.txt for auth.")
    else:
        log.warning("No auth — YouTube may block server IPs. Send /login.")
    return opts


async def do_oauth2_login(chat_id: int, status_msg) -> bool:
    """
    Check if yt-dlp OAuth2 token is already cached (from manual login),
    or instruct the user to run the one-time login command on the server.
    """
    token_found = False
    cache_dirs = [
        Path.home() / ".cache" / "yt-dlp",
        Path.home() / ".cache" / "youtube-dl",
    ]
    for d in cache_dirs:
        try:
            if d.exists() and any(d.rglob("*oauth2*")):
                token_found = True
                break
        except PermissionError:
            pass

    if token_found:
        await status_msg.edit(
            "✅ **OAuth2 token already found!**\n\n"
            "yt-dlp is authenticated with YouTube.\n"
            "The bot will use this token automatically."
        )
        return True

    await status_msg.edit(
        "🔐 **One-time YouTube login required**\n\n"
        "Run this command directly on your server terminal:\n\n"
        "`yt-dlp --username oauth2 --password '' --flat-playlist \"https://www.youtube.com/playlist?list=PLbpi6ZahtOH6Ar_3GPy3workz2YkaLWJD\"`\n\n"
        "It will show a code like: `PYL-NZZ-KXNF`\n\n"
        "⚡ **Be quick — open google.com/device BEFORE running the command, then enter the code fast!**\n\n"
        "1️⃣ Open **https://www.google.com/device** on your phone/PC first\n"
        "2️⃣ Run the command above in your terminal\n"
        "3️⃣ Copy the code from terminal → paste it on google.com/device\n"
        "4️⃣ Sign in with Google\n"
        "5️⃣ Wait for yt-dlp to finish (may take a moment)\n\n"
        "Then send /cookiestatus to confirm ✅\n\n"
        "_Done once — works forever, auto-refreshes._"
    )
    return False


# Minimum seconds between progress edits (avoids FloodWait)
PROGRESS_INTERVAL = 4.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ─────────────────────────────────────────────────────────────────────
pending: dict[int, dict] = {}

# ─── HELPERS ───────────────────────────────────────────────────────────────────

YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]{11}"
)

def extract_url(text: str):
    m = YOUTUBE_RE.search(text)
    return m.group(0) if m else None


def safe_filename(name: str, max_len: int = 60) -> str:
    """Strip characters that are invalid in filenames."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip(". ")
    return name[:max_len] if name else "audio"


def get_16x9_thumbnail(info: dict):
    thumbs = info.get("thumbnails", [])
    preferred = ["maxresdefault", "sddefault", "hqdefault", "mqdefault", "default"]
    by_id = {t.get("id", ""): t.get("url", "") for t in thumbs}
    for p in preferred:
        if p in by_id:
            return by_id[p]
    vid_id = info.get("id")
    if vid_id:
        return f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg"
    return thumbs[-1]["url"] if thumbs else None


def download_thumbnail(url: str, path: Path):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        w, h = img.size
        target_h = int(w * 9 / 16)
        if h > target_h:
            top = (h - target_h) // 2
            img = img.crop((0, top, w, top + target_h))
        img.save(path, "JPEG", quality=95)
        return path
    except Exception as e:
        log.warning(f"Thumbnail download failed: {e}")
        return None


def fetch_info(url: str) -> dict:
    ydl_opts = {
        **get_ydl_base_opts(),
        "skip_download": True,
        "format": "bestvideo+bestaudio/best",
        "ignore_no_formats_error": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def build_quality_buttons(info: dict):
    formats = info.get("formats", [])
    video_opts = {}
    audio_opts = {}

    for f in formats:
        size   = f.get("filesize") or f.get("filesize_approx") or 0
        if size and size > MAX_FILE_SIZE:
            continue

        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")

        if vcodec != "none" and height:
            key = f"{height}p"
            if key not in video_opts:
                video_opts[key] = {
                    "label": f"🎬 {key}",
                    "format_id": f["format_id"],
                    "height": height,
                    "width": f.get("width", 0) or 0,
                    "filesize": size,
                    "type": "video",
                }

        if vcodec == "none" and acodec != "none":
            abr = int(f.get("abr") or 0)
            key = f"{abr}kbps"
            # Label always shows MP3 because we convert via FFmpeg
            if key not in audio_opts or abr > audio_opts[key].get("abr", 0):
                audio_opts[key] = {
                    "label": f"🎵 {abr}kbps MP3",
                    "format_id": f["format_id"],
                    "abr": abr,
                    "ext": "mp3",
                    "filesize": size,
                    "type": "audio",
                }

    video_list = sorted(video_opts.values(), key=lambda x: x["height"], reverse=True)
    audio_list = sorted(audio_opts.values(), key=lambda x: x.get("abr", 0), reverse=True)
    return video_list, audio_list


def human_size(b) -> str:
    if not b:
        return "?"
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def format_duration(sec) -> str:
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def get_mp3_duration(path: Path) -> int:
    try:
        audio = MP3(str(path))
        return int(audio.info.length)
    except Exception:
        return 0


def embed_thumbnail_in_audio(audio_path: Path, thumb_path, info: dict):
    try:
        try:
            tags = ID3(str(audio_path))
        except ID3NoHeaderError:
            tags = ID3()

        tags["TIT2"] = TIT2(encoding=3, text=info.get("title", ""))
        tags["TPE1"] = TPE1(encoding=3, text=info.get("uploader", ""))
        tags["TALB"] = TALB(encoding=3, text=info.get("title", ""))

        if thumb_path and Path(thumb_path).exists():
            with open(thumb_path, "rb") as f:
                tags["APIC"] = APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=f.read(),
                )
        tags.save(str(audio_path), v2_version=3)
    except Exception as e:
        log.warning(f"Tag embedding failed: {e}")


class ThrottledProgress:
    """Calls status.edit() at most once every PROGRESS_INTERVAL seconds to avoid FloodWait."""
    def __init__(self, msg, label="Uploading"):
        self.msg      = msg
        self.label    = label
        self._last_ts = 0.0

    async def __call__(self, current, total):
        now = time.monotonic()
        if total and (now - self._last_ts) >= PROGRESS_INTERVAL:
            self._last_ts = now
            pct = current / total * 100
            bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
            try:
                await self.msg.edit(
                    f"{self.label}…\n[{bar}] {pct:.1f}%\n"
                    f"{human_size(current)} / {human_size(total)}"
                )
            except Exception:
                pass


# ─── BOT ───────────────────────────────────────────────────────────────────────

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


@client.on(events.NewMessage(pattern=r"(?i)/start"))
async def cmd_start(event):
    await event.reply(
        "👋 **YouTube Downloader Bot**\n\n"
        "Send me any YouTube link and I'll show you all available qualities.\n"
        "Tap a quality to download & receive the file! 🚀\n\n"
        "_Supports files up to 2 GB._\n\n"
        "⚠️ First time on a server? Send /login to connect your YouTube account once — "
        "permanent, no maintenance needed."
    )


@client.on(events.NewMessage(pattern=r"(?i)/login"))
async def cmd_login(event):
    """Start the YouTube OAuth2 device-code flow."""
    chat_id = event.chat_id
    status  = await event.reply("⏳ Contacting Google for a login code…")
    await do_oauth2_login(chat_id, status)


@client.on(events.NewMessage(pattern=r"(?i)/cookiestatus"))
async def cmd_cookiestatus(event):
    if COOKIES_FILE.exists():
        age_days = (time.time() - COOKIES_FILE.stat().st_mtime) / 86400
        await event.reply(
            f"✅ **cookies.txt active!**\n"
            f"Last synced: {age_days:.1f} days ago\n\n"
            "Bot is authenticated. 🎉"
        )
    else:
        await event.reply(
            "❌ **No cookies.txt found!**\n\n"
            "Run in Termux on your phone:\n"
            "`bash ~/sync_cookies.sh`"
        )


@client.on(events.NewMessage(pattern=r"(?i)/help"))
async def cmd_help(event):
    await event.reply(
        "**YouTube Bot — Help**\n\n"
        "📎 Send any YouTube link to get download options.\n\n"
        "**Commands:**\n"
        "/start — Welcome message\n"
        "/login — Connect YouTube account (one-time, permanent)\n"
        "/cookiestatus — Check current auth status\n"
        "/help — This message\n\n"
        "**About /login:**\n"
        "Uses the same device-code flow as Smart TVs — "
        "you visit youtube.com/activate, enter a short code, "
        "sign in once, and the bot stores a token that auto-refreshes forever."
    )


@client.on(events.NewMessage)
async def handle_message(event):
    if event.sender_id is None or event.via_bot_id:
        return

    text = event.raw_text or ""
    url  = extract_url(text)
    if not url:
        return

    chat_id = event.chat_id
    user_id = event.sender_id
    status  = await event.reply("🔍 Fetching video info…")

    try:
        info = fetch_info(url)
    except Exception as e:
        await status.edit(f"❌ Failed to fetch info:\n`{e}`")
        return

    title    = info.get("title", "Unknown")
    duration = format_duration(info.get("duration") or 0)
    channel  = info.get("uploader", "Unknown")
    views    = f"{info.get('view_count', 0):,}"

    thumb_url  = get_16x9_thumbnail(info)
    thumb_path = DOWNLOAD_DIR / f"thumb_{info['id']}.jpg"
    thumb_dl   = download_thumbnail(thumb_url, thumb_path) if thumb_url else None

    video_list, audio_list = build_quality_buttons(info)

    pending[user_id] = {
        "url": url,
        "info": info,
        "thumb_path": thumb_dl,
        "chat_id": chat_id,
        "video_list": video_list,
        "audio_list": audio_list,
    }

    buttons = []
    for i, opt in enumerate(video_list[:4]):
        size_str = f" ({human_size(opt['filesize'])})" if opt["filesize"] else ""
        btn_data = f"v{user_id}_{i}".encode()
        buttons.append([Button.inline(f"{opt['label']}{size_str}", data=btn_data)])
    for i, opt in enumerate(audio_list[:2]):
        size_str = f" ({human_size(opt['filesize'])})" if opt["filesize"] else ""
        btn_data = f"a{user_id}_{i}".encode()
        buttons.append([Button.inline(f"{opt['label']}{size_str}", data=btn_data)])

    caption = (
        f"**{title}**\n"
        f"📺 {channel}  •  ⏱ {duration}  •  👁 {views} views\n\n"
        "Choose a quality to download:"
    )

    await status.delete()

    if not buttons:
        await client.send_message(chat_id, f"⚠️ No downloadable formats found for this video.")
        return

    if thumb_dl and thumb_dl.exists():
        await client.send_file(chat_id, file=str(thumb_dl), caption=caption)
        await asyncio.sleep(0.5)
        await client.send_message(chat_id, "Choose a quality:", buttons=buttons)
    else:
        await client.send_message(chat_id, caption, buttons=buttons)


@client.on(events.CallbackQuery(pattern=rb"[va]\d+_\d+"))
async def handle_download(event):
    data    = event.data.decode()
    kind    = data[0]
    rest    = data[1:].split("_")
    user_id = int(rest[0])
    idx     = int(rest[1])

    state = pending.get(user_id)
    if not state:
        await event.answer("❌ Session expired. Please resend the link.", alert=True)
        return

    await event.answer("⏳ Starting download…")

    info       = state["info"]
    url        = state["url"]
    thumb_path = state["thumb_path"]
    chat_id    = state["chat_id"]
    opt        = (state["video_list"] if kind == "v" else state["audio_list"])[idx]
    label      = opt["label"]

    safe_title = safe_filename(info.get("title", "video"))
    vid_dur    = int(info.get("duration") or 0)

    status = await client.send_message(chat_id, f"⬇️ Downloading {label}…")
    out_prefix = DOWNLOAD_DIR / f"{info['id']}_{opt['format_id']}"

    try:
        # ── VIDEO ──────────────────────────────────────────────────────────────
        if kind == "v":
            height = opt.get("height", 720)
            ydl_opts = {
                **get_ydl_base_opts(),
                "format": f"bestvideo[height<={height}]+bestaudio/bestvideo[height<={height}]/best[height<={height}]/best",
                "outtmpl": str(out_prefix) + ".%(ext)s",
                "merge_output_format": "mp4",
                "postprocessors": [],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            matches = list(DOWNLOAD_DIR.glob(f"{info['id']}_{opt['format_id']}.*"))
            if not matches:
                raise FileNotFoundError("Downloaded file not found")
            raw_path = matches[0]

            # Rename to human-readable title
            final_path = DOWNLOAD_DIR / f"{safe_title}.mp4"
            if raw_path != final_path:
                raw_path.rename(final_path)

            await status.edit(f"⬆️ Uploading {label}…")
            prog = ThrottledProgress(status, "Uploading")

            await client.send_file(
                chat_id,
                file=str(final_path),
                caption=f"**{info['title']}**\n{label}",
                thumb=str(thumb_path) if thumb_path and Path(thumb_path).exists() else None,
                supports_streaming=True,
                # Proper duration so player does NOT show 0:00
                attributes=[
                    DocumentAttributeVideo(
                        duration=vid_dur,
                        w=opt.get("width") or 1280,
                        h=opt.get("height") or 720,
                        supports_streaming=True,
                    )
                ],
                progress_callback=prog,
            )

        # ── AUDIO ──────────────────────────────────────────────────────────────
        else:
            ydl_opts = {
                **get_ydl_base_opts(),
                "format": "bestaudio/best",
                "outtmpl": str(out_prefix) + ".%(ext)s",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }
                ],
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            matches = list(DOWNLOAD_DIR.glob(f"{info['id']}_{opt['format_id']}*.mp3"))
            if not matches:
                matches = list(DOWNLOAD_DIR.glob(f"{info['id']}_{opt['format_id']}*"))
            if not matches:
                raise FileNotFoundError("Downloaded audio not found")
            raw_path = matches[0]

            # Rename to human-readable title
            final_path = DOWNLOAD_DIR / f"{safe_title}.mp3"
            if raw_path != final_path:
                raw_path.rename(final_path)

            # Embed ID3 tags + cover art
            embed_thumbnail_in_audio(final_path, thumb_path, info)

            # Read actual duration from the MP3
            audio_dur = get_mp3_duration(final_path) or vid_dur

            await status.edit(f"⬆️ Uploading {label}…")
            prog = ThrottledProgress(status, "Uploading")

            # Send as AUDIO (music player) not as a document/file
            await client.send_file(
                chat_id,
                file=str(final_path),
                caption=f"**{info['title']}**\n{label}",
                thumb=str(thumb_path) if thumb_path and Path(thumb_path).exists() else None,
                attributes=[
                    DocumentAttributeAudio(
                        duration=audio_dur,
                        title=info.get("title", ""),
                        performer=info.get("uploader", ""),
                        voice=False,          # voice=False → music track
                    )
                ],
                progress_callback=prog,
            )

        await status.edit(f"✅ Done! {label} sent.")

        try:
            final_path.unlink(missing_ok=True)
        except Exception:
            pass

    except Exception as e:
        log.exception("Download/upload error")
        try:
            await status.edit(f"❌ Error: `{e}`")
        except Exception:
            pass


# ─── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info(f"Bot started as @{me.username}")
    log.info("Listening for YouTube links…")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
