import os
import re
import csv
import io
import json
import time
import base64
import sqlite3
import requests
from urllib.parse import urlparse, parse_qs, quote
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, Response

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "terastream_secure_session_key_2026")
PORT = int(os.environ.get("PORT", 8080))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "akempire")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ak@empire")
DB_FILE = os.path.join(os.path.dirname(__file__), "terastream.db")

# Lifetime Cloud Database (PostgreSQL) configuration
DEFAULT_DATABASE_URL = "postgresql://neondb_owner:npg_0JyzHhFLNGs1@ep-gentle-bonus-ae1k78jw-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
DATABASE_URL = os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


# ----------------- LIFETIME PERSISTENT DATABASE ENGINE -----------------

def get_db_connection():
    """Returns a database connection (PostgreSQL if DATABASE_URL is set, otherwise local SQLite)."""
    if DATABASE_URL and HAS_PSYCOPG2:
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
            return "postgres", conn
        except Exception as e:
            print("PostgreSQL connection error, falling back to SQLite:", e)

    conn = sqlite3.connect(DB_FILE, timeout=15)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return "sqlite", conn


def init_db():
    """Initializes the search_logs table across PostgreSQL and SQLite."""
    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS search_logs (
                        id SERIAL PRIMARY KEY,
                        searched_url TEXT,
                        surl TEXT,
                        video_title TEXT,
                        video_size TEXT,
                        stream_url TEXT,
                        download_url TEXT,
                        user_ip TEXT,
                        user_agent TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
            else:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS search_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        searched_url TEXT,
                        surl TEXT,
                        video_title TEXT,
                        video_size TEXT,
                        stream_url TEXT,
                        download_url TEXT,
                        user_ip TEXT,
                        user_agent TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
            conn.commit()
        conn.close()
    except Exception as e:
        print("DB Init Error:", e)

init_db()


def log_search(searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent):
    """Guarantees every user search query is saved to lifetime persistent database."""
    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute('''
                    INSERT INTO search_logs (searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    str(searched_url or ''),
                    str(surl or ''),
                    str(video_title or 'TeraBox Video'),
                    str(video_size or 'HD Video'),
                    str(stream_url or ''),
                    str(download_url or ''),
                    str(user_ip or '127.0.0.1'),
                    str(user_agent or 'Unknown')
                ))
            else:
                cursor.execute('''
                    INSERT INTO search_logs (searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    str(searched_url or ''),
                    str(surl or ''),
                    str(video_title or 'TeraBox Video'),
                    str(video_size or 'HD Video'),
                    str(stream_url or ''),
                    str(download_url or ''),
                    str(user_ip or '127.0.0.1'),
                    str(user_agent or 'Unknown')
                ))
            conn.commit()
        conn.close()
    except Exception as e:
        print("Log search error:", e)


# ----------------- IN-MEMORY RESOLUTION CACHE -----------------
RESOLVE_CACHE = {}


# ----------------- SESSION CACHE FOR HLS STREAMING -----------------

FLOW_SESSION = {
    "session": None,
    "csrf_token": None,
    "last_init": 0
}


def get_flow_session():
    """Maintains an active, initialized device session for direct HLS stream extraction."""
    now = time.time()
    if FLOW_SESSION["session"] and FLOW_SESSION["csrf_token"] and (now - FLOW_SESSION["last_init"] < 1200):
        return FLOW_SESSION["session"], FLOW_SESSION["csrf_token"]

    sess = requests.Session()
    sess.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Ch-Ua': '"Chromium";v="124", "Not(A:Brand";v="24", "Google Chrome";v="124"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1'
    })

    try:
        r_home = sess.get('https://flowvideoplayer.com', timeout=8)
        csrf_match = re.search(r'name="csrf-token"\s+content="([^"]+)"', r_home.text)
        if not csrf_match:
            return None, None
        
        csrf_token = csrf_match.group(1)

        device_data = {
            "cpu": 8,
            "memory": 8,
            "touch": 0,
            "platform": "Win32",
            "lang": "en-US",
            "vendor": "Google Inc.",
            "webgl_vendor": "Google Inc. (NVIDIA)",
            "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "backup_token": None,
            "os": "windows",
            "browser": "chrome",
            "pwa_installed": False
        }

        ajax_headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': 'https://flowvideoplayer.com',
            'Referer': 'https://flowvideoplayer.com/',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'X-CSRF-TOKEN': csrf_token,
            'X-Requested-With': 'XMLHttpRequest',
        }

        r_init = sess.post('https://flowvideoplayer.com/device/init', json=device_data, headers=ajax_headers, timeout=8)
        if r_init.status_code == 200:
            FLOW_SESSION["session"] = sess
            FLOW_SESSION["csrf_token"] = csrf_token
            FLOW_SESSION["last_init"] = now
            return sess, csrf_token
    except Exception as e:
        print("Session init err:", e)

    return None, None


def extract_surl(raw_input: str) -> str:
    """Extracts the clean shorturl (surl) from ANY TeraBox / TeraShare / TeraBoxLink format."""
    if not raw_input:
        return ""
    
    # Strip all inner and outer whitespaces
    clean_input = re.sub(r'\s+', '', raw_input.strip())
    
    # 1. Query parameters (?surl=... or ?shorturl=... or ?key=...)
    if "?" in clean_input:
        try:
            parsed = urlparse(clean_input)
            qs = parse_qs(parsed.query)
            for k in ["surl", "shorturl", "key"]:
                if k in qs and qs[k]:
                    surl = qs[k][0]
                    return surl[1:] if surl.startswith("1") else surl
        except Exception:
            pass

    # 2. Regex for /s/1... or /s/...
    match = re.search(r'/s/(?:1)?([a-zA-Z0-9_-]{10,40})', clean_input)
    if match:
        return match.group(1)

    # 3. Regex for surl=...
    match_surl = re.search(r'surl=(?:1)?([a-zA-Z0-9_-]{10,40})', clean_input)
    if match_surl:
        return match_surl.group(1)

    # 4. Regex for /share/link, /sharing/link, or /share/file
    match_share = re.search(r'/share(?:ing)?/(?:link|file|init)?.*?1?([a-zA-Z0-9_-]{10,40})', clean_input)
    if match_share:
        return match_share.group(1)

    # 5. Raw key format
    match_raw = re.search(r'\b1?([a-zA-Z0-9_-]{15,40})\b', clean_input)
    if match_raw:
        val = match_raw.group(1)
        return val[1:] if val.startswith("1") else val

    return ""


FLARE_AES_KEY = b"CMrhmcd9oFUjWBBleiMfS0BiBfupaVsG"
FLARE_AES_IV = b"2Xk4dLo38c9Z2Q2a"


def decrypt_flare_data(encrypted_b64: str, key_str: str = None) -> str:
    """Decrypts AES-CBC stream data from Flare/CashSnap using IV 2Xk4dLo38c9Z2Q2a."""
    if not HAS_CRYPTO or not encrypted_b64:
        return ""
    try:
        clean_ciphertext = encrypted_b64.strip('"').strip()
        raw_cipher = base64.b64decode(clean_ciphertext)

        keys_to_try = [FLARE_AES_KEY]
        if key_str:
            keys_to_try.insert(0, key_str.encode('utf-8'))

        for k in keys_to_try:
            try:
                cipher = AES.new(k, AES.MODE_CBC, FLARE_AES_IV)
                decrypted = unpad(cipher.decrypt(raw_cipher), AES.block_size)
                res = decrypted.decode('utf-8')
                if res.startswith("http"):
                    return res
            except Exception:
                pass

        for k in keys_to_try:
            try:
                cipher = AES.new(k, AES.MODE_ECB)
                decrypted = unpad(cipher.decrypt(raw_cipher), AES.block_size)
                res = decrypted.decode('utf-8')
                if res.startswith("http"):
                    return res
            except Exception:
                pass

        return ""
    except Exception as e:
        print("Decrypt flare data error:", e)
        return ""


def extract_flare_id(raw_input: str) -> str:
    """Extracts numeric link ID from Flare / Flaredvns / CashSnap links, URLs, or raw IDs."""
    if not raw_input:
        return ""
    clean = raw_input.strip()

    # 1. Query parameter (?linkId=... or ?link_id=... or ?id=...)
    m1 = re.search(r'[?&#](?:linkId|link_id|linkid|id)=(\d{15,25})', clean, re.IGNORECASE)
    if m1:
        return m1.group(1)

    # 2. Path pattern like /s/2096112993141268482 or /share/2096112993141268482
    m2 = re.search(r'/(?:s|play|v|share|link)/(\d{15,25})', clean, re.IGNORECASE)
    if m2:
        return m2.group(1)

    # 3. Flare / CashSnap / HugeBox / BlinkNote / Flaredvns domains containing numbers
    if any(k in clean.lower() for k in ["flare", "hugebox", "cashsnap", "flaredvns", "blinknote", "cshsnp"]):
        m3 = re.search(r'(\d{15,25})', clean)
        if m3:
            return m3.group(1)

    # 4. Pure numeric ID (16 to 25 digits)
    m4 = re.search(r'\b(\d{16,25})\b', clean)
    if m4:
        return m4.group(1)

    return ""


def is_flare_link(raw_input: str) -> bool:
    """Detects if input is a Flare / CashSnap / HugeBox / Flaredvns link or numeric link ID."""
    return bool(extract_flare_id(raw_input))


def resolve_flare_stream(raw_url_or_id: str) -> dict:
    """Resolves video streams and playlists from Flare / Flaredvns / CashSnap links."""
    clean_id = extract_flare_id(raw_url_or_id)
    if not clean_id:
        return {"success": False, "error": "Please enter a valid Flare / Flaredvns link or Link ID."}

    now = time.time()
    cache_key = f"flare_{clean_id}"
    if cache_key in RESOLVE_CACHE:
        cached_time, cached_res = RESOLVE_CACHE[cache_key]
        if now - cached_time < 1200:
            return cached_res

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Content-Type': 'application/json',
        'Origin': 'https://www.flarewliv.com',
        'Referer': f'https://www.flarewliv.com/?linkId={clean_id}'
    }

    api_hosts = [
        "https://api.cshsnpcwio.com",
        "https://api.cashsnapnowhawk.com"
    ]

    files = []
    for host in api_hosts:
        try:
            r = requests.post(
                f"{host}/v1/h5/share/link/files/page",
                json={"link_id": clean_id, "page": 0, "size": 20, "file_type": "FILE"},
                headers=headers,
                timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("files"):
                    files = data["files"]
                    break
        except Exception:
            continue

    if not files:
        return {
            "success": False,
            "error": f"Flare link (ID: {clean_id}) has expired, been deleted by the owner, or is no longer accessible.",
            "surl": clean_id,
            "mode": "flare"
        }

    playlist = []
    for idx, f in enumerate(files):
        file_id = f.get("file_id") or f.get("id")
        namespace = f.get("namespace") or {}
        uid = namespace.get("name") or namespace.get("id") or f.get("uid") or ""
        file_meta = f.get("file_meta") or {}

        title = file_meta.get("display_name") or f.get("file_name") or f"Flare Video {idx+1}"
        raw_size = file_meta.get("size") or f.get("file_size") or 0
        size_str = f"{raw_size / (1024 * 1024):.1f} MB" if isinstance(raw_size, (int, float)) and raw_size > 0 else "HD Video"
        thumbnail = file_meta.get("thumbnail") or f.get("thumbnail") or ""

        stream_url = None
        for host in api_hosts:
            try:
                r_dl = requests.post(f"{host}/v1/h5/download_file_url", json={"uid": uid, "file_id": file_id}, headers=headers, timeout=8)
                if r_dl.status_code == 200 and r_dl.text:
                    enc_data = r_dl.text.strip('"').strip()
                    if enc_data:
                        stream_url = decrypt_flare_data(enc_data)
                        if stream_url:
                            break
            except Exception:
                continue

        playlist.append({
            "index": idx,
            "title": title,
            "size": size_str,
            "thumbnail": thumbnail,
            "stream_url": stream_url,
            "proxy_stream_url": f"/api/stream/proxy?url={quote(stream_url, safe='')}" if stream_url and ".m3u8" in stream_url else None,
            "download_url": stream_url
        })

    first = playlist[0]
    result = {
        "success": True,
        "surl": clean_id,
        "full_surl": clean_id,
        "title": first["title"],
        "size": first["size"],
        "size_bytes": 0,
        "duration_str": "Full HD",
        "thumbnail": first["thumbnail"],
        "stream_url": first["stream_url"],
        "proxy_stream_url": first.get("proxy_stream_url"),
        "download_url": first["download_url"],
        "is_hls": bool(first["stream_url"] and ".m3u8" in first["stream_url"]),
        "mode": "flare",
        "playlist": playlist
    }
    RESOLVE_CACHE[cache_key] = (time.time(), result)
    return result






def is_direct_stream(raw_input: str) -> bool:
    """Detects if input is a direct video (.mp4, .m3u8, .webm) URL."""
    if not raw_input:
        return False
    clean = raw_input.strip().lower()
    return clean.startswith("http") and any(ext in clean for ext in [".m3u8", ".mp4", ".webm", ".mkv", ".mov"])


def resolve_direct_stream(raw_input: str) -> dict:
    """Prepares direct streaming player payload for MP4 / M3U8 links."""
    clean = raw_input.strip()
    is_hls = ".m3u8" in clean.lower()
    parsed_path = urlparse(clean).path
    filename = os.path.basename(parsed_path) or "Direct Stream Video"

    return {
        "success": True,
        "surl": "direct",
        "full_surl": "direct",
        "title": f"Direct: {filename}",
        "size": "Direct Stream",
        "size_bytes": 0,
        "duration_str": "HD Quality",
        "thumbnail": None,
        "stream_url": clean,
        "proxy_stream_url": f"/api/stream/proxy?url={quote(clean, safe='')}" if is_hls else None,
        "download_url": clean if not is_hls else None,
        "is_hls": is_hls,
        "mode": "direct",
        "playlist": [{
            "index": 0,
            "title": filename,
            "size": "Direct Video",
            "thumbnail": None,
            "stream_url": clean,
            "proxy_stream_url": f"/api/stream/proxy?url={quote(clean, safe='')}" if is_hls else None,
            "download_url": clean if not is_hls else None
        }]
    }


# ----------------- YOUTUBE RESOLVER ENGINE -----------------

def extract_youtube_id(raw_input: str) -> str:
    """Extracts YouTube 11-char video ID from any YouTube URL format, shorts, youtu.be, or raw ID."""
    if not raw_input:
        return ""
    clean = raw_input.strip()

    # 1. Query parameter (?v=... or &v=...)
    if "v=" in clean:
        m = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', clean)
        if m:
            return m.group(1)

    # 2. Path formats (youtu.be/..., /shorts/..., /embed/..., /live/..., /v/..., /e/...)
    m_path = re.search(r'(?:youtu\.be\/|\/shorts\/|\/embed\/|\/live\/|\/v\/|\/e\/)([a-zA-Z0-9_-]{11})', clean)
    if m_path:
        return m_path.group(1)

    # 3. Pure 11-char ID
    m_raw = re.match(r'^[a-zA-Z0-9_-]{11}$', clean)
    if m_raw:
        return clean

    return ""


def is_youtube_link(raw_input: str) -> bool:
    """Detects if input is a YouTube video, shorts, or video ID."""
    if not raw_input:
        return False
    clean = raw_input.lower().strip()
    if any(k in clean for k in ["youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"]):
        return True
    return bool(extract_youtube_id(raw_input))


def resolve_youtube_stream(raw_url_or_id: str) -> dict:
    """Resolves YouTube video stream, metadata, thumbnail, duration, and multi-quality download options."""
    video_id = extract_youtube_id(raw_url_or_id)
    target_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else raw_url_or_id.strip()

    now = time.time()
    cache_key = f"yt_{video_id or target_url}"
    if cache_key in RESOLVE_CACHE:
        cached_time, cached_res = RESOLVE_CACHE[cache_key]
        if now - cached_time < 1200:  # 20 minutes cache
            return cached_res

    if not HAS_YTDLP:
        return {
            "success": False,
            "error": "YouTube extraction engine (yt-dlp) is not installed on the server.",
            "mode": "youtube"
        }

    vid = video_id or "youtube"
    title = f"YouTube Video {vid}"
    duration_str = "HD Video"
    uploader = "YouTube"
    views_str = ""
    thumbnail = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid != "youtube" else None
    formats = []

    # 1. Extract progressive format (format 18) via android client
    try:
        ydl_opts_prog = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'socket_timeout': 15,
            'format': None,
            'extractor_args': {'youtube': {'player_client': ['android']}}
        }
        with yt_dlp.YoutubeDL(ydl_opts_prog) as ydl_prog:
            info = ydl_prog.extract_info(target_url, download=False)
            if info:
                vid = info.get('id') or vid
                title = info.get('title') or title
                duration_secs = info.get('duration') or 0
                if duration_secs >= 3600:
                    duration_str = f"{int(duration_secs // 3600):02d}:{int((duration_secs % 3600) // 60):02d}:{int(duration_secs % 60):02d}"
                elif duration_secs > 0:
                    duration_str = f"{int(duration_secs // 60):02d}:{int(duration_secs % 60):02d}"
                uploader = info.get('uploader') or info.get('channel') or uploader
                views = info.get('view_count')
                if views:
                    views_str = f"{views:,} views"
                thumbnail = info.get('thumbnail') or thumbnail
                formats.extend(info.get('formats', []))
    except Exception as e:
        print("Android extraction notice:", e)

    # 2. Extract HD and Audio formats via android_creator
    try:
        ydl_opts_hd = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'socket_timeout': 15,
            'format': None,
            'extractor_args': {'youtube': {'player_client': ['android_creator']}}
        }
        with yt_dlp.YoutubeDL(ydl_opts_hd) as ydl_hd:
            info_hd = ydl_hd.extract_info(target_url, download=False)
            if info_hd and info_hd.get('formats'):
                formats.extend(info_hd['formats'])
                if title == f"YouTube Video {vid}" and info_hd.get('title'):
                    title = info_hd['title']
    except Exception as e:
        print("Android creator extraction notice:", e)

    try:
        if not formats:
            raise Exception("No formats or metadata retrieved from YouTube")

        # 1. Progressive formats (both video and audio)
        prog_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url')]
        best_prog = None
        if prog_formats:
            best_prog = sorted(prog_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True)[0]

        stream_url = best_prog['url'] if best_prog else None
        proxy_stream_url = f"/api/youtube/stream?v={vid}&itag={best_prog.get('format_id', '')}" if best_prog else None

        # 2. Build multi-quality download options
        download_formats = []
        seen_resolutions = set()

        # Progressive MP4
        if best_prog:
            h = best_prog.get('height') or '360'
            size_bytes = best_prog.get('filesize') or best_prog.get('filesize_approx') or 0
            size_label = f"{size_bytes / (1024*1024):.1f} MB" if size_bytes else "HD"
            download_formats.append({
                "label": f"⚡ {h}p Fast MP4 (Video + Audio)",
                "quality": f"{h}p MP4",
                "format_id": best_prog.get('format_id'),
                "ext": best_prog.get('ext', 'mp4'),
                "size": size_label,
                "download_url": f"/api/youtube/download?v={vid}&itag={best_prog.get('format_id')}&title={quote(title)}",
                "is_progressive": True
            })
            seen_resolutions.add(f"{h}p")

        # HD Video formats (1080p, 720p, 480p, 360p, etc.)
        video_only_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('url')]
        sorted_video = sorted(video_only_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True)

        for f in sorted_video:
            h = f.get('height')
            if not h:
                continue
            res_key = f"{h}p"
            if res_key in seen_resolutions:
                continue
            seen_resolutions.add(res_key)
            size_bytes = f.get('filesize') or f.get('filesize_approx') or 0
            size_label = f"{size_bytes / (1024*1024):.1f} MB" if size_bytes else "HD"
            ext = f.get('ext', 'mp4')
            download_formats.append({
                "label": f"📺 {res_key} HD Video ({ext.upper()})",
                "quality": f"{res_key}",
                "format_id": f.get('format_id'),
                "ext": ext,
                "size": size_label,
                "download_url": f"/api/youtube/download?v={vid}&itag={f.get('format_id')}&title={quote(title)}",
                "is_progressive": f.get('acodec') != 'none'
            })

        # Audio formats (M4A / MP3)
        audio_formats = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none' and f.get('url')]
        if audio_formats:
            best_audio = sorted(audio_formats, key=lambda x: (x.get('abr') or x.get('tbr') or 0), reverse=True)[0]
            size_bytes = best_audio.get('filesize') or best_audio.get('filesize_approx') or 0
            size_label = f"{size_bytes / (1024*1024):.1f} MB" if size_bytes else "Audio"
            download_formats.append({
                "label": f"🎵 High Quality Audio (M4A / MP3)",
                "quality": "Audio MP3/M4A",
                "format_id": best_audio.get('format_id'),
                "ext": best_audio.get('ext', 'm4a'),
                "size": size_label,
                "download_url": f"/api/youtube/download?v={vid}&itag={best_audio.get('format_id')}&title={quote(title)}",
                "is_progressive": True
            })

        primary_dl = download_formats[0]["download_url"] if download_formats else (stream_url or f"https://www.youtube.com/watch?v={vid}")

        result = {
            "success": True,
            "surl": vid,
            "full_surl": vid,
            "title": title,
            "channel": uploader,
            "views": views_str,
            "size": duration_str,
            "size_bytes": 0,
            "duration_str": duration_str,
            "thumbnail": thumbnail,
            "stream_url": stream_url,
            "proxy_stream_url": proxy_stream_url,
            "download_url": primary_dl,
            "download_formats": download_formats,
            "is_hls": False,
            "embed_url": f"https://www.youtube-nocookie.com/embed/{vid}?autoplay=1",
            "mode": "youtube",
            "playlist": [{
                "index": 0,
                "title": title,
                "size": duration_str,
                "thumbnail": thumbnail,
                "stream_url": stream_url,
                "proxy_stream_url": proxy_stream_url,
                "download_url": primary_dl
            }]
        }

        RESOLVE_CACHE[cache_key] = (time.time(), result)
        return result

    except Exception as e:
        print("YouTube extraction notice:", e)
        # Ultra-reliable fallback via oEmbed + YouTube embed player
        fallback_title = "YouTube Video"
        fallback_thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None
        try:
            r_oembed = requests.get(f"https://www.youtube.com/oembed?url={quote(target_url)}&format=json", timeout=5)
            if r_oembed.status_code == 200:
                oe_data = r_oembed.json()
                fallback_title = oe_data.get('title') or fallback_title
                fallback_thumb = oe_data.get('thumbnail_url') or fallback_thumb
        except Exception:
            pass

        vid = video_id or "youtube"
        embed_result = {
            "success": True,
            "surl": vid,
            "full_surl": vid,
            "title": fallback_title,
            "channel": "YouTube",
            "views": "",
            "size": "HD Stream",
            "size_bytes": 0,
            "duration_str": "HD Stream",
            "thumbnail": fallback_thumb,
            "stream_url": None,
            "proxy_stream_url": None,
            "download_url": f"https://www.youtube.com/watch?v={vid}",
            "download_formats": [],
            "is_hls": False,
            "embed_url": f"https://www.youtube-nocookie.com/embed/{vid}?autoplay=1",
            "mode": "youtube",
            "playlist": [{
                "index": 0,
                "title": fallback_title,
                "size": "HD Video",
                "thumbnail": fallback_thumb,
                "stream_url": None,
                "proxy_stream_url": None,
                "download_url": f"https://www.youtube.com/watch?v={vid}"
            }]
        }
        RESOLVE_CACHE[cache_key] = (time.time(), embed_result)
        return embed_result


def resolve_universal_stream(raw_input: str, mode: str = None) -> dict:
    """Intelligently routes any input to YouTube, TeraBox, Flare/CashSnap, or Direct Video player."""
    clean = raw_input.strip()
    if not clean:
        return {"success": False, "error": "Please enter a valid link."}

    # 1. YouTube mode or YouTube link format
    if mode == "youtube" or is_youtube_link(clean):
        return resolve_youtube_stream(clean)

    # 2. Flare mode or Flare link format (flaredvns, flare*, hugebox, or numeric link ID)
    if mode == "flare" or is_flare_link(clean):
        return resolve_flare_stream(clean)

    # 3. Direct video stream (.mp4, .m3u8, .webm)
    if mode == "direct" or (is_direct_stream(clean) and not any(k in clean.lower() for k in ["terabox", "1024tera", "terashare", "flare", "hugebox", "cashsnap", "youtube", "youtu.be"])):
        return resolve_direct_stream(clean)

    # 5. TeraBox / 1024Tera / TeraShare links
    return resolve_terabox_stream(clean)



def resolve_terabox_stream(raw_url_or_surl: str) -> dict:
    """Extracts direct HLS stream (.m3u8), download link, thumbnails, and metadata with high-speed caching."""
    clean_surl = extract_surl(raw_url_or_surl)
    if not clean_surl:
        return {"success": False, "error": "Please enter a valid TeraBox share link."}

    # Check cache first for instant sub-millisecond response
    now = time.time()
    if clean_surl in RESOLVE_CACHE:
        cached_time, cached_res = RESOLVE_CACHE[clean_surl]
        if now - cached_time < 1200:  # 20 minutes cache
            return cached_res

    full_surl = f"1{clean_surl}"

    result = {
        "success": True,
        "surl": clean_surl,
        "full_surl": full_surl,
        "title": "TeraBox Video Stream",
        "size": "HD 720p / 1080p",
        "size_bytes": 0,
        "duration_str": "Full Length",
        "thumbnail": None,
        "stream_url": None,
        "download_url": None,
        "is_hls": False,
        "embed_url": f"https://www.terabox.app/sharing/embed?surl={full_surl}",
        "mode": "terabox",
        "playlist": []
    }

    session_obj, csrf_token = get_flow_session()
    if session_obj and csrf_token:
        try:
            ajax_headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Origin': 'https://flowvideoplayer.com',
                'Referer': 'https://flowvideoplayer.com/',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'X-CSRF-TOKEN': csrf_token,
                'X-Requested-With': 'XMLHttpRequest',
            }

            # Top high-performing canonical formats for instant resolution
            candidate_links = [
                f"https://teraboxlink.com/s/1{clean_surl}",
                f"https://1024tera.com/s/1{clean_surl}",
                f"https://www.terabox.app/wap/share/filelist?surl={clean_surl}"
            ]

            clean_raw = raw_url_or_surl.strip().replace(" ", "")
            if clean_raw.startswith("http") and "wap/share" in clean_raw:
                candidate_links.insert(0, clean_raw)

            for target_link in candidate_links:
                try:
                    resp = session_obj.post('https://flowvideoplayer.com/search/video', json={'url': target_link}, headers=ajax_headers, timeout=8)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") is True and data.get("response") and len(data["response"]) > 0:
                            video_list = data["response"]
                            first_video = video_list[0]

                            result["title"] = first_video.get("file_name") or "TeraBox Video Stream"
                            result["size"] = first_video.get("file_size") or "HD Video"
                            result["size_bytes"] = first_video.get("file_size_bytes") or 0
                            result["thumbnail"] = first_video.get("thumbnail")
                            result["stream_url"] = first_video.get("fast_stream_url")
                            result["download_url"] = first_video.get("download_url")
                            result["proxy_stream_url"] = f"/api/stream/proxy?url={quote(result['stream_url'], safe='')}" if result["stream_url"] else None
                            result["is_hls"] = bool(result["stream_url"])

                            playlist = []
                            for idx, v in enumerate(video_list):
                                item_stream = v.get("fast_stream_url")
                                playlist.append({
                                    "index": idx,
                                    "title": v.get("file_name") or f"Part {idx+1}",
                                    "size": v.get("file_size") or "",
                                    "thumbnail": v.get("thumbnail"),
                                    "stream_url": item_stream,
                                    "proxy_stream_url": f"/api/stream/proxy?url={quote(item_stream, safe='')}" if item_stream else None,
                                    "download_url": v.get("download_url")
                                })
                            result["playlist"] = playlist

                            # Save to memory cache for instant future loads
                            RESOLVE_CACHE[clean_surl] = (time.time(), result)
                            return result
                except Exception as inner_e:
                    continue
        except Exception as e:
            print("Direct HLS extraction error:", e)

    # If stream could not be extracted (expired/deleted link)
    if not result.get("stream_url"):
        result["success"] = False
        result["error"] = "This TeraBox video link has expired, been deleted by the owner, or is no longer available on TeraBox."

    return result


# ----------------- USER PUBLIC ROUTES -----------------

@app.route('/')
def index():
    query_url = request.args.get('url') or request.args.get('surl')
    if query_url:
        info = resolve_universal_stream(query_url)
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
        user_agent = request.headers.get('User-Agent', 'Unknown')
        title_to_log = info.get("title") if info.get("success") else f"[Unresolved / Expired: {info.get('error', 'Error')}]"
        log_search(
            searched_url=query_url,
            surl=info.get("surl") or extract_youtube_id(query_url) or extract_surl(query_url) or extract_flare_id(query_url),
            video_title=title_to_log,
            video_size=info.get("size", "HD Video"),
            stream_url=info.get("stream_url", ""),
            download_url=info.get("download_url", ""),
            user_ip=user_ip,
            user_agent=user_agent
        )
        return render_template(
            "index.html",
            initial_data=info if info.get("success") else None,
            initial_url=query_url
        )
    
    return render_template(
        "index.html",
        initial_data=None,
        initial_url=""
    )


@app.route('/play/<surl>')
def play_surl(surl):
    info = resolve_universal_stream(surl)
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
    user_agent = request.headers.get('User-Agent', 'Unknown')
    title_to_log = info.get("title") if info.get("success") else f"[Unresolved / Expired: {info.get('error', 'Error')}]"
    
    searched_url = surl
    if not is_flare_link(surl) and not is_youtube_link(surl):
        searched_url = f"https://teraboxshare.com/s/1{surl}"

    log_search(
        searched_url=searched_url,
        surl=info.get("surl") or surl,
        video_title=title_to_log,
        video_size=info.get("size", "HD Video"),
        stream_url=info.get("stream_url", ""),
        download_url=info.get("download_url", ""),
        user_ip=user_ip,
        user_agent=user_agent
    )
    return render_template(
        "index.html",
        initial_data=info if info.get("success") else None,
        initial_url=surl
    )


@app.route('/api/resolve', methods=['GET', 'POST'])
def api_resolve():
    raw_input = ""
    mode = ""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        raw_input = data.get('url') or data.get('surl') or ""
        mode = data.get('mode') or ""
    else:
        raw_input = request.args.get('url') or request.args.get('surl') or ""
        mode = request.args.get('mode') or ""

    if not raw_input:
        return jsonify({"success": False, "error": "No link provided. Please paste a video link."}), 400

    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
    user_agent = request.headers.get('User-Agent', 'Unknown')

    try:
        stream_info = resolve_universal_stream(raw_input, mode=mode)
    except Exception as e:
        print("Resolve error:", e)
        stream_info = {"success": False, "error": f"Server error while resolving link: {e}"}
    if not isinstance(stream_info, dict):
        stream_info = {"success": False, "error": "Could not resolve this link. Please try again."}

    # Guaranteed logging for every search query (success or failed/expired)
    title_to_log = stream_info.get("title") if stream_info.get("success") else f"[Unresolved / Expired: {stream_info.get('error', 'Error')}]"
    log_search(
        searched_url=raw_input,
        surl=stream_info.get("surl") or extract_youtube_id(raw_input) or extract_surl(raw_input) or extract_flare_id(raw_input),
        video_title=title_to_log,
        video_size=stream_info.get("size", "HD Video"),
        stream_url=stream_info.get("stream_url", ""),
        download_url=stream_info.get("download_url", ""),
        user_ip=user_ip,
        user_agent=user_agent
    )

    return jsonify(stream_info)


@app.route('/api/youtube/stream')
def youtube_stream_proxy():
    """Proxies YouTube media stream chunks with Range header support for fast buffer & seeking."""
    video_id = request.args.get('v') or request.args.get('id')
    itag = request.args.get('itag')
    if not video_id:
        return Response("Missing video ID", status=400)

    # Resolve or use cached info
    cache_key = f"yt_{video_id}"
    resolved = None
    if cache_key in RESOLVE_CACHE:
        _, resolved = RESOLVE_CACHE[cache_key]
    else:
        resolved = resolve_youtube_stream(video_id)

    if not resolved or not resolved.get("success"):
        return Response("Could not resolve video stream", status=404)

    target_url = resolved.get("stream_url")
    if not target_url:
        return Response("No streamable format found", status=404)

    req_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    if 'Range' in request.headers:
        req_headers['Range'] = request.headers['Range']

    try:
        r = requests.get(target_url, headers=req_headers, stream=True, timeout=25)
        def generate():
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        resp = Response(generate(), status=r.status_code, content_type=r.headers.get('content-type', 'video/mp4'))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
        if 'Content-Range' in r.headers:
            resp.headers['Content-Range'] = r.headers['Content-Range']
        if 'Content-Length' in r.headers:
            resp.headers['Content-Length'] = r.headers['Content-Length']
        if 'Accept-Ranges' in r.headers:
            resp.headers['Accept-Ranges'] = r.headers['Accept-Ranges']
        return resp
    except Exception as e:
        return Response(f"Stream proxy error: {str(e)}", status=500)


@app.route('/api/youtube/download')
def youtube_download():
    """Directly streams video/audio file attachment with clean filename and maximum download speed."""
    video_id = request.args.get('v') or request.args.get('id')
    itag = request.args.get('itag')
    title = request.args.get('title') or f"YouTube_Video_{video_id}"

    if not video_id:
        return Response("Missing video ID", status=400)

    # Sanitize title for filename
    clean_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or f"youtube_{video_id}"

    target_url = None
    ext = "mp4"
    formats = []
    try:
        # Try android client first
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
            'socket_timeout': 15,
            'format': None,
            'extractor_args': {'youtube': {'player_client': ['android']}}
        }) as ydl_and:
            info_and = ydl_and.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if info_and and info_and.get('formats'):
                formats.extend(info_and['formats'])

        # If specific itag requested and not in android formats, try android_creator
        if itag and not any(str(f.get('format_id')) == str(itag) for f in formats):
            with yt_dlp.YoutubeDL({
                'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
                'format': None,
                'extractor_args': {'youtube': {'player_client': ['android_creator']}}
            }) as ydl_hd:
                info_hd = ydl_hd.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                if info_hd and info_hd.get('formats'):
                    formats.extend(info_hd['formats'])

        matched_format = None
        if itag:
            for f in formats:
                if str(f.get('format_id')) == str(itag):
                    matched_format = f
                    break
        if not matched_format:
            progs = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url')]
            matched_format = progs[0] if progs else (formats[-1] if formats else None)

        if matched_format:
            target_url = matched_format.get('url')
            ext = matched_format.get('ext', 'mp4')
    except Exception as e:
        print("YouTube download extract error:", e)

    if not target_url:
        return Response("Failed to resolve download URL", status=500)

    # Stream the file directly as an attachment
    req_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    try:
        r = requests.get(target_url, headers=req_headers, stream=True, timeout=30)
        def generate():
            for chunk in r.iter_content(chunk_size=131072):
                if chunk:
                    yield chunk

        content_type = r.headers.get('content-type', 'video/mp4')
        resp = Response(generate(), status=r.status_code, content_type=content_type)
        resp.headers['Content-Disposition'] = f'attachment; filename="{clean_title}.{ext}"'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        if 'Content-Length' in r.headers:
            resp.headers['Content-Length'] = r.headers['Content-Length']
        return resp
    except Exception as e:
        return Response(f"Download stream error: {str(e)}", status=500)


@app.route('/api/stream/proxy')
def stream_proxy():
    """Proxies HLS .m3u8 playlists and video streams with full CORS headers."""
    target_url = request.args.get('url')
    if not target_url:
        return Response("Missing URL parameter", status=400)

    req_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://flowvideoplayer.com/'
    }
    if 'Range' in request.headers:
        req_headers['Range'] = request.headers['Range']

    try:
        if '.m3u8' in target_url or 'get_m3u8' in target_url:
            r = requests.get(target_url, headers=req_headers, timeout=12)
            content = r.text
            base_url = target_url.rsplit('/', 1)[0] + '/'
            lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith('#') and not stripped.startswith('http'):
                    full_segment_url = base_url + stripped
                    lines.append(f"/api/stream/proxy?url={quote(full_segment_url, safe='')}")
                else:
                    lines.append(line)
            rewritten_m3u8 = '\n'.join(lines).encode('utf-8')
            resp = Response(rewritten_m3u8, status=r.status_code, content_type="application/vnd.apple.mpegurl; charset=utf-8")
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
            resp.headers['Cache-Control'] = 'no-cache'
            return resp
        else:
            r = requests.get(target_url, headers=req_headers, stream=True, timeout=20)
            def generate():
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk

            resp = Response(generate(), status=r.status_code, content_type=r.headers.get('content-type', 'video/MP2T'))
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
            if 'Content-Range' in r.headers:
                resp.headers['Content-Range'] = r.headers['Content-Range']
            if 'Content-Length' in r.headers:
                resp.headers['Content-Length'] = r.headers['Content-Length']
            if 'Accept-Ranges' in r.headers:
                resp.headers['Accept-Ranges'] = r.headers['Accept-Ranges']
            return resp
    except Exception as e:
        return Response(f"Proxy stream error: {str(e)}", status=500)


@app.errorhandler(500)
def handle_500(e):
    if request.path.startswith('/api/'):
        return jsonify({"success": False, "error": "Internal server error. Please try again."}), 500
    return e, 500


@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith('/api/'):
        return jsonify({"success": False, "error": "Not found"}), 404
    return e, 404


@app.route('/api/health')
def health():
    return jsonify({"status": "ok", "service": "TeraStream Pro Native", "port": PORT})


# ----------------- ADMIN PANEL ROUTES -----------------

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))

    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            error = "Invalid admin username or password."

    return render_template('admin_login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
def admin_dashboard():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    logs = []
    stats = {
        "total_searches": 0,
        "unique_links": 0,
        "today_searches": 0,
        "unique_ips": 0,
        "db_type": "SQLite (Local File)"
    }

    try:
        db_type, conn = get_db_connection()
        stats["db_type"] = "Cloud PostgreSQL (Lifetime Persistent)" if db_type == "postgres" else "SQLite (Local File)"

        if db_type == "postgres" and HAS_PSYCOPG2:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute('SELECT COUNT(*) AS count FROM search_logs')
            stats["total_searches"] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(DISTINCT surl) AS count FROM search_logs')
            stats["unique_links"] = cursor.fetchone()['count']

            cursor.execute("SELECT COUNT(*) AS count FROM search_logs WHERE created_at >= NOW() - INTERVAL '1 day'")
            stats["today_searches"] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(DISTINCT user_ip) AS count FROM search_logs')
            stats["unique_ips"] = cursor.fetchone()['count']

            cursor.execute('SELECT * FROM search_logs ORDER BY id DESC LIMIT 500')
            logs = cursor.fetchall()
        else:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('SELECT COUNT(*) FROM search_logs')
            stats["total_searches"] = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(DISTINCT surl) FROM search_logs')
            stats["unique_links"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM search_logs WHERE created_at >= datetime('now', '-1 day')")
            stats["today_searches"] = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(DISTINCT user_ip) FROM search_logs')
            stats["unique_ips"] = cursor.fetchone()[0]

            cursor.execute('SELECT * FROM search_logs ORDER BY id DESC LIMIT 500')
            logs = [dict(row) for row in cursor.fetchall()]

        conn.close()
    except Exception as e:
        print("Admin fetch error:", e)

    feed_ctx = {}
    try:
        feed_ctx = get_feed_context()
        stats.update(feed_ctx.get("feed_stats", {}))
    except Exception as e:
        print("Admin feed context error:", e)

    return render_template(
        'admin.html', logs=logs, stats=stats,
        feed_sources=feed_ctx.get("feed_sources", []),
        feed_videos=feed_ctx.get("feed_videos", []),
        auto_sync=feed_ctx.get("auto_sync"),
        post_targets=feed_ctx.get("post_targets", []),
        auto_post=feed_ctx.get("auto_post"),
    )


@app.route('/admin/export/csv')
def admin_export_csv():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Timestamp', 'Video Title', 'Size', 'Searched URL', 'SURL', 'Stream URL', 'Download URL', 'User IP', 'User Agent'])

    try:
        db_type, conn = get_db_connection()
        if db_type == "postgres" and HAS_PSYCOPG2:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

        cursor.execute('SELECT * FROM search_logs ORDER BY id DESC')
        for row in cursor.fetchall():
            writer.writerow([
                row['id'], str(row['created_at']), row['video_title'], row['video_size'],
                row['searched_url'], row['surl'], row['stream_url'], row['download_url'],
                row['user_ip'], row['user_agent']
            ])
        conn.close()
    except Exception as e:
        print("CSV export error:", e)

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=terastream_user_searches.csv"}
    )


@app.route('/admin/delete/<int:log_id>')
def admin_delete(log_id):
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute('DELETE FROM search_logs WHERE id = %s', (log_id,))
            else:
                cursor.execute('DELETE FROM search_logs WHERE id = ?', (log_id,))
            conn.commit()
        conn.close()
    except Exception as e:
        print("Delete error:", e)

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/clear')
def admin_clear():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM search_logs')
            conn.commit()
        conn.close()
    except Exception as e:
        print("Clear error:", e)

    return redirect(url_for('admin_dashboard'))


# ----------------- ADMIN FEED / TELEGRAM / AUTO-SYNC ROUTES -----------------
from admin_feed import register_admin_feed
get_feed_context = register_admin_feed(app, get_db_connection, extract_surl, extract_youtube_id)


if __name__ == '__main__':
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print("==================================================")
    print(f"TeraStream Pro running at: http://127.0.0.1:{PORT}")
    print(f"Admin Panel: http://127.0.0.1:{PORT}/admin")
    print("==================================================")
    app.run(host='127.0.0.1', port=PORT, debug=False, threaded=True)
