import os
import re
import tempfile
from urllib.parse import urlparse
from flask import (
    Flask, request, render_template_string, abort,
    send_file, after_this_request
)
import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)

# ---------- Config ----------
MAX_BYTES = 300 * 1024 * 1024  # 300MB limit
MAX_DURATION_SEC = 10 * 60     # 10 minutes max
CHUNK = 1024 * 1024            # 1 MB chunks
ALLOWED_SCHEMES = {"http", "https"}
TIMEOUT = (5, 60)

HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Universal Video Downloader</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; }
    h1 { font-size: 1.5rem; }
    form { display: grid; gap: 12px; margin-top: 16px; }
    input[type="url"], input[type="file"], select {
      padding: 10px; font-size: 1rem; width: 100%;
    }
    button { padding: 10px 14px; font-size: 1rem; cursor: pointer; }
    .note { color: #555; font-size: .95rem; }
  </style>
</head>
<body>
  <h1>Universal Video Downloader</h1>
  <p class="note">Paste any direct video URL or a link from Instagram, TikTok, Facebook, YouTube, etc.</p>

  <form method="POST" action="/download" enctype="multipart/form-data">
    <input type="url" name="url" placeholder="https://www.facebook.com/share/v/..." required>
    <div>
      <select name="quality">
        <option value="best">Best Available</option>
        <option value="720">Up to 720p</option>
        <option value="480">Up to 480p</option>
        <option value="360">Up to 360p</option>
      </select>
    </div>
    <input type="file" name="cookies" accept=".txt">
    <button type="submit">Download</button>
  </form>

  <p class="note">Limits: {{ max_min }} minutes · {{ max_mb }} MB</p>
</body>
</html>
"""

def _safe_filename(name, default="video"):
    name = (name or default).strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not re.search(r"\.(mp4|mov|webm|mkv)$", name, re.I):
        name += ".mp4"
    return name

def _pick_format(formats, pref):
    playable = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    mp4s = [f for f in playable if f.get("ext") == "mp4"]
    def score(f): return (f.get("height") or 0, f.get("tbr") or 0)
    target_h = int(pref) if pref.isdigit() else None
    chosen = None
    if mp4s:
        sorted_mp4s = sorted(mp4s, key=score)
        if target_h:
            under = [f for f in sorted_mp4s if (f.get("height") or 0) <= target_h]
            chosen = under[-1] if under else sorted_mp4s[0]
        else:
            chosen = sorted_mp4s[-1]
    else:
        sorted_all = sorted(playable, key=score)
        if target_h:
            under = [f for f in sorted_all if (f.get("height") or 0) <= target_h]
            chosen = under[-1] if under else (sorted_all[0] if sorted_all else None)
        else:
            chosen = sorted_all[-1] if sorted_all else None
    return chosen

@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML,
        max_min=MAX_DURATION_SEC // 60,
        max_mb=MAX_BYTES // (1024*1024))

@app.route("/download", methods=["POST"])
def download():
    url = (request.form.get("url") or "").strip()
    quality = (request.form.get("quality") or "best").strip().lower()
    parsed = urlparse(url)

    if not url or parsed.scheme.lower() not in ALLOWED_SCHEMES:
        abort(400, "Invalid URL. Must start with http/https.")

    # Cookies if provided
    cookiefile = None
    if "cookies" in request.files and request.files["cookies"].filename:
        tmp_cookie = tempfile.NamedTemporaryFile(delete=False)
        request.files["cookies"].save(tmp_cookie.name)
        cookiefile = tmp_cookie.name

    # Use yt-dlp for everything: works for both direct links and social links
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        if cookiefile: os.unlink(cookiefile)
        abort(400, f"Could not process URL: {e}")

    if cookiefile:
        os.unlink(cookiefile)

    # Handle playlists
    if "entries" in info and info["entries"]:
        info = info["entries"][0]

    # Check duration
    duration = info.get("duration") or 0
    if duration and duration > MAX_DURATION_SEC:
        abort(413, f"Video too long ({duration}s). Limit is {MAX_DURATION_SEC}s.")

    # Pick format
    chosen = _pick_format(info.get("formats"), quality)
    if not chosen or not chosen.get("url"):
        abort(415, "No downloadable video format found.")

    # Check size if known
    est_size = chosen.get("filesize") or chosen.get("filesize_approx")
    if est_size and est_size > MAX_BYTES:
        abort(413, f"File too large ({est_size//(1024*1024)} MB). Limit is {MAX_BYTES//(1024*1024)} MB.")

    # Stream to temp file
    try:
        r = requests.get(chosen["url"], stream=True, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        abort(504, f"Error fetching video: {e}")

    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp_path = tmp.name
    written = 0
    try:
        for chunk in r.iter_content(chunk_size=CHUNK):
            if not chunk:
                continue
            written += len(chunk)
            if written > MAX_BYTES:
                tmp.close()
                os.unlink(tmp_path)
                abort(413, "Exceeded file size limit.")
            tmp.write(chunk)
        tmp.flush()
        tmp.close()
    except Exception:
        tmp.close()
        os.unlink(tmp_path)
        abort(500, "Error during download.")

    @after_this_request
    def _cleanup(resp):
        try: os.unlink(tmp_path)
        except: pass
        return resp

    filename = _safe_filename(info.get("title"))
    return send_file(tmp_path, as_attachment=True, download_name=filename, mimetype="video/mp4")

if __name__ == "__main__":
    app.run(debug=True)
