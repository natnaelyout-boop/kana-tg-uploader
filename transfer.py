import os
import re
import time
import subprocess
import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
STRING_SESSION = os.environ["TG_STRING_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BIN_CHANNEL_ID = int(os.environ["BIN_CHANNEL_ID"])
GH_TOKEN = os.environ["GH_MEDIA_TOKEN"]
MESSAGE_ID = int(os.environ["MESSAGE_ID"])
CHAT_ID = os.environ["CHAT_ID"]
CUSTOM_NAME = os.environ.get("CUSTOM_NAME", "").strip()

CF_PROXY_BASE = "https://github-video-warmer.natnaelyout.workers.dev/?url="

GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}

def send_tg(text):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )

def clean_filename(raw_name, fallback_name):
    base = raw_name or fallback_name or f"video_{int(time.time())}.mp4"
    ext_match = re.search(r"(\.[a-zA-Z0-9]{2,5})$", base)
    ext = ext_match.group(1).lower() if ext_match else ".mp4"
    stem = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", base)
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", stem).strip("_")
    if not stem:
        stem = f"episode_{int(time.time())}"
    return stem + ext

def optimize_video(input_path):
    """Compresses video to fast-loading ~95-110 MB MP4 with +faststart."""
    if not input_path.lower().endswith(".mp4"):
        return input_path
    base, _ = os.path.splitext(input_path)
    fast_path = f"{base}_fast.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_path,
        "-vf", "scale='min(854,iw)':-2",
        "-c:v", "libx264",
        "-crf", "26",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "80k",
        "-movflags", "+faststart",
        fast_path,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode == 0 and os.path.exists(fast_path) and os.path.getsize(fast_path) > 0:
        os.remove(input_path)
        return fast_path
    return input_path

def get_or_create_target_repo():
    repos = requests.get(
        "https://api.github.com/user/repos?per_page=100&sort=created&direction=desc",
        headers=GH_HEADERS,
        timeout=30,
    ).json()

    for r in repos:
        if r["private"] or any(k in r["name"].lower() for k in ("thumb", "image", "poster", "uploader")):
            continue
        rel_res = requests.get(f"https://api.github.com/repos/{r['full_name']}/releases/tags/v1.0", headers=GH_HEADERS, timeout=30)
        if rel_res.status_code == 200:
            rel = rel_res.json()
            if len(rel.get("assets", [])) < 10:
                return r["full_name"], rel["id"]
        elif rel_res.status_code == 404:
            created_rel = requests.post(
                f"https://api.github.com/repos/{r['full_name']}/releases",
                headers=GH_HEADERS,
                json={"tag_name": "v1.0", "name": "Media Assets v1.0"},
                timeout=30,
            ).json()
            return r["full_name"], created_rel["id"]

    new_name = f"kanamovies_{int(time.time())}"
    new_repo = requests.post(
        "https://api.github.com/user/repos",
        headers=GH_HEADERS,
        json={"name": new_name, "private": False, "auto_init": True},
        timeout=30,
    ).json()
    time.sleep(2)
    new_rel = requests.post(
        f"https://api.github.com/repos/{new_repo['full_name']}/releases",
        headers=GH_HEADERS,
        json={"tag_name": "v1.0", "name": "Media Assets v1.0"},
        timeout=30,
    ).json()
    return new_repo["full_name"], new_rel["id"]

def main():
    try:
        with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
            client.get_dialogs()
            entity = client.get_entity(BIN_CHANNEL_ID)
            msg = client.get_messages(entity, ids=MESSAGE_ID)
            if not msg or not msg.media:
                send_tg("Error: Could not find the video in BIN_CHANNEL.")
                return

            orig_name = getattr(msg.file, "name", "") or "episode.mp4"
            final_name = clean_filename(CUSTOM_NAME, orig_name)
            orig_mb = round((msg.file.size or 0) / (1024 * 1024), 1)

            send_tg(f"Downloading {final_name} ({orig_mb} MB) on GitHub runner...")
            local_path = client.download_media(msg, file=final_name)

        send_tg("Optimizing video for fast streaming...")
        upload_path = optimize_video(local_path)
        final_mb = round(os.path.getsize(upload_path) / (1024 * 1024), 1)

        full_repo, release_id = get_or_create_target_repo()
        send_tg(f"Uploading {final_name} ({final_mb} MB) to {full_repo}...")

        upload_url = f"https://uploads.github.com/repos/{full_repo}/releases/{release_id}/assets?name={final_name}"
        with open(upload_path, "rb") as f:
            up_res = requests.post(
                upload_url,
                headers={
                    "Authorization": f"Bearer {GH_TOKEN}",
                    "Content-Type": "video/mp4",
                    "Accept": "application/vnd.github+json",
                },
                data=f,
                timeout=3600,
            )

        data = up_res.json()
        if up_res.status_code in (200, 201) and "browser_download_url" in data:
            raw_github_url = data["browser_download_url"]
            cf_stream_url = f"{CF_PROXY_BASE}{raw_github_url}"
            send_tg(
                f"Done! ({orig_mb} MB -> {final_mb} MB)\n"
                f"Repo: {full_repo}\n\n"
                f"Fast Stream Link (Paste in Website):\n{cf_stream_url}\n\n"
                f"Direct GitHub Link:\n{raw_github_url}"
            )
        else:
            send_tg(f"GitHub Upload Failed ({up_res.status_code}): {data}")
    except Exception as e:
        send_tg(f"Transfer Error: {e}")

if __name__ == "__main__":
    main()
