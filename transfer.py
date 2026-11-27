import os
import re
import time
import asyncio
import subprocess
import requests
from telethon import TelegramClient

# Environment Variables from GitHub Secrets & Dispatch Payload
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BIN_CHANNEL = int(os.environ["BIN_CHANNEL"])
GH_TOKEN = os.environ.get("GH_MEDIA_TOKEN") or os.environ.get("GH_DISPATCH_TOKEN")
MESSAGE_ID = int(os.environ["MESSAGE_ID"])
CHAT_ID = int(os.environ["CHAT_ID"])

REPO_PREFIX = "kanamovies_"
RELEASE_TAG = "v1.0"
MAX_ASSETS_PER_REPO = 10

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def send_telegram_message(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )


def sanitize_filename(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not clean.lower().endswith(".mp4"):
        clean += ".mp4"
    return clean


def optimize_video(input_path: str) -> str:
    """Keeps original resolution (576p), optimizes bitrate, and adds +faststart for instant playback."""
    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_fast.mp4"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-c:v", "libx264",
        "-crf", "24",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        output_path,
    ]

    print(f"Optimizing {input_path} with +faststart...")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        orig_mb = os.path.getsize(input_path) / (1024 * 1024)
        new_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"Optimization complete: {orig_mb:.1f} MB -> {new_mb:.1f} MB")
        os.remove(input_path)
        return output_path

    print("FFmpeg failed, falling back to original file:", result.stderr[-400:])
    return input_path


def get_github_username() -> str:
    r = requests.get("https://api.github.com/user", headers=GH_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()["login"]


def get_or_create_target_release(username: str):
    """Finds an existing kanamovies_* public repo with less than 10 assets, or creates a new one."""
    r = requests.get(
        "https://api.github.com/user/repos?per_page=100&sort=created&direction=desc",
        headers=GH_HEADERS,
        timeout=30,
    )
    r.raise_for_status()

    for repo in r.json():
        repo_name = repo["name"]
        if repo_name.startswith(REPO_PREFIX) and not repo["private"]:
            rel_url = f"https://api.github.com/repos/{username}/{repo_name}/releases/tags/{RELEASE_TAG}"
            rel_res = requests.get(rel_url, headers=GH_HEADERS, timeout=30)
            if rel_res.status_code == 200:
                rel_data = rel_res.json()
                if len(rel_data.get("assets", [])) < MAX_ASSETS_PER_REPO:
                    return repo_name, rel_data["upload_url"].split("{")[0]

    new_repo_name = f"{REPO_PREFIX}{int(time.time())}"
    create_repo_res = requests.post(
        "https://api.github.com/user/repos",
        headers=GH_HEADERS,
        json={"name": new_repo_name, "private": False, "auto_init": True},
        timeout=30,
    )
    create_repo_res.raise_for_status()
    time.sleep(2)

    create_rel_res = requests.post(
        f"https://api.github.com/repos/{username}/{new_repo_name}/releases",
        headers=GH_HEADERS,
        json={"tag_name": RELEASE_TAG, "name": "Media Storage v1.0"},
        timeout=30,
    )
    create_rel_res.raise_for_status()
    upload_url = create_rel_res.json()["upload_url"].split("{")[0]
    return new_repo_name, upload_url


def upload_to_github(file_path: str) -> str:
    username = get_github_username()
    repo_name, upload_url = get_or_create_target_release(username)

    asset_name = f"{int(time.time())}_{os.path.basename(file_path)}"
    upload_headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Content-Type": "video/mp4",
    }

    with open(file_path, "rb") as f:
        res = requests.post(
            f"{upload_url}?name={asset_name}",
            headers=upload_headers,
            data=f,
            timeout=600,
        )
    res.raise_for_status()
    return res.json()["browser_download_url"]


async def main():
    client = TelegramClient("bot_session", API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    try:
        send_telegram_message(CHAT_ID, "⏳ Downloading video from Telegram...")
        message = await client.get_messages(BIN_CHANNEL, ids=MESSAGE_ID)
        if not message or not message.media:
            send_telegram_message(CHAT_ID, "❌ Error: Video message not found in BIN_CHANNEL.")
            return

        raw_name = getattr(message.file, "name", None) or f"episode_{MESSAGE_ID}.mp4"
        local_filename = sanitize_filename(raw_name)
        downloaded_path = await client.download_media(message, file=local_filename)

        send_telegram_message(CHAT_ID, "⚙️ Optimizing 576p video for fast streaming...")
        optimized_path = optimize_video(downloaded_path)

        send_telegram_message(CHAT_ID, "☁️ Uploading optimized video to GitHub...")
        direct_url = upload_to_github(optimized_path)

        final_mb = os.path.getsize(optimized_path) / (1024 * 1024)
        send_telegram_message(
            CHAT_ID,
            f"✅ Upload Complete!\n\n📦 Size: {final_mb:.1f} MB\n🔗 Direct Link:\n{direct_url}",
        )
    except Exception as e:
        send_telegram_message(CHAT_ID, f"❌ Upload failed:\n{str(e)}")
        raise
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
