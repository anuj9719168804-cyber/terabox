"""
Admin feed backend for TeraStream Pro.

Adds every /admin/api/* route that templates/admin.html calls:
  - feed sources (add / toggle / delete / sync)
  - Auto Sync (background monitoring of all active sources)
  - Telegram login (send_code / verify_code / disconnect / status)
  - Telegram dialogs, resolve link, add source, import group videos
  - feed videos (import link / delete / clear)

Works with both PostgreSQL (DATABASE_URL) and local SQLite.
Telegram needs api_id + api_hash (enter them in the admin Telegram Setup form,
or set TELEGRAM_API_ID / TELEGRAM_API_HASH environment variables).
"""
import os
import re
import json
import time
import asyncio
import threading

import requests
from flask import request, jsonify, session

try:
    from telethon import TelegramClient, functions, types, errors
    from telethon.sessions import StringSession
    HAS_TELETHON = True
except Exception:  # telethon not installed
    HAS_TELETHON = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except Exception:
    HAS_BS4 = False

DEFAULT_SOURCES = [
    ("Telegram 1", "https://t.me/c/4403722928"),
    ("Telegram 2", "https://t.me/+URv-kcSD4PViYzJl"),
    ("Telegram 3", "https://t.me/+8HBZ7OyNArdlMjA9"),
    ("Telegram 4", "https://t.me/+5iv846o72rY0MGM1"),
]

TERA_HINTS = [
    "terabox", "terashare", "mirrobox", "nephobox", "4funbox", "1024tera",
    "teraboxapp", "teraboxlink", "tibibox", "momerybox", "freeterabox",
    "terafileshare", "teraboxshare", "flare", "hugebox", "cashsnap",
]
URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')
SURL_RE = re.compile(r'/s/(?:1)?([a-zA-Z0-9_-]{10,40})')


def register_admin_feed(app, get_db_connection, extract_surl, extract_youtube_id):
    """Attach all admin feed routes and start the Auto Sync worker."""

    # ------------------------------------------------------------------ DB helpers
    def db(sql, params=(), fetch=None):
        """Run one statement. fetch: None -> rowcount, 'one' -> dict|None, 'all' -> [dict]."""
        db_type, conn = get_db_connection()
        try:
            cur = conn.cursor()
            if db_type == "postgres":
                sql = sql.replace("?", "%s")
            cur.execute(sql, params)
            result = cur.rowcount
            if fetch:
                cols = [d[0] for d in cur.description]
                if fetch == "all":
                    result = [dict(zip(cols, r)) for r in cur.fetchall()]
                else:
                    row = cur.fetchone()
                    result = dict(zip(cols, row)) if row else None
            conn.commit()
            return result
        finally:
            conn.close()

    def is_pg():
        db_type, conn = get_db_connection()
        conn.close()
        return db_type == "postgres"

    def ensure_tables():
        pg = is_pg()
        pk = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        db(f'''CREATE TABLE IF NOT EXISTS feed_sources (
            id {pk}, name TEXT NOT NULL, url TEXT UNIQUE NOT NULL,
            is_active INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_synced_at TIMESTAMP)''')
        db(f'''CREATE TABLE IF NOT EXISTS feed_videos (
            id {pk}, source_id INTEGER, source_name TEXT, title TEXT NOT NULL,
            video_url TEXT UNIQUE NOT NULL, thumbnail_url TEXT, surl TEXT,
            discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        db(f'''CREATE TABLE IF NOT EXISTS telegram_auth (
            id {pk}, api_id INTEGER, api_hash TEXT, session_string TEXT NOT NULL,
            phone TEXT, user_name TEXT, is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        db('''CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    def get_setting(key, default=None):
        row = db("SELECT value FROM app_settings WHERE key = ?", (key,), "one")
        return row["value"] if row else default

    def set_setting(key, value):
        db("DELETE FROM app_settings WHERE key = ?", (key,))
        db("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))

    def seed_default_sources():
        """One-time: if no sources exist yet, add the default Telegram sources."""
        if get_setting("sources_seeded"):
            return
        if not db("SELECT id FROM feed_sources LIMIT 1", (), "one"):
            for name, url in DEFAULT_SOURCES:
                insert_ignore("feed_sources", ("name", "url", "is_active"), (name, url, 1))
        set_setting("sources_seeded", "1")

    def insert_ignore(table, cols, vals):
        marks = ",".join("?" * len(cols))
        if is_pg():
            sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks}) ON CONFLICT DO NOTHING"
        else:
            sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({marks})"
        return db(sql, vals)

    def add_video(title, video_url, surl="", source_id=None, source_name="", thumb=""):
        """Insert a feed video; returns True if it was new."""
        n = insert_ignore(
            "feed_videos",
            ("source_id", "source_name", "title", "video_url", "thumbnail_url", "surl"),
            (source_id, source_name, (title or "Untitled Video")[:200], video_url, thumb or "", surl or ""),
        )
        return bool(n and n > 0)

    # ------------------------------------------------------------------ auth guard
    def admin_only():
        if not session.get("is_admin"):
            return jsonify({"success": False, "error": "Admin login required. Please log in again."}), 401
        return None

    def jerr(msg, code=200):
        return jsonify({"success": False, "error": str(msg)}), code

    # ------------------------------------------------------------------ link extraction
    def is_video_link(link):
        low = link.lower()
        return any(k in low for k in TERA_HINTS) or bool(SURL_RE.search(link) and "t.me" not in low)

    def clean_link(link):
        return link.rstrip(".,;:!?)")

    def make_title(text, fallback):
        lines = [l.strip() for l in (text or "").split("\n") if l.strip() and not l.strip().lower().startswith("http")]
        return (lines[0] if lines else fallback)[:200]

    def extract_videos_from_text(text, extra_links=(), fallback_title="Telegram Video"):
        found, seen = [], set()
        links = [clean_link(l) for l in URL_RE.findall(text or "")] + [clean_link(l) for l in extra_links if l]
        title = make_title(text, fallback_title)
        for link in links:
            if link in seen or not is_video_link(link):
                continue
            seen.add(link)
            found.append({"title": title, "video_url": link, "surl": extract_surl(link) or ""})
        return found

    def extract_videos_from_message(msg, fallback_title):
        text = getattr(msg, "message", None) or getattr(msg, "text", "") or ""
        extra = [e.url for e in (getattr(msg, "entities", None) or []) if getattr(e, "url", None)]
        return extract_videos_from_text(text, extra, fallback_title)

    # ------------------------------------------------------------------ Telegram plumbing
    _loop_holder = {"loop": None}
    _login = {}

    def get_loop():
        if _loop_holder["loop"] is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, daemon=True).start()
            _loop_holder["loop"] = loop
        return _loop_holder["loop"]

    def run_async(coro, timeout=90):
        return asyncio.run_coroutine_threadsafe(coro, get_loop()).result(timeout)

    def need_telethon():
        if not HAS_TELETHON:
            raise RuntimeError("Telethon is not installed on the server. Add 'telethon' to requirements.txt and redeploy.")

    def get_auth():
        return db("SELECT * FROM telegram_auth WHERE is_active = 1 ORDER BY id DESC LIMIT 1", (), "one")

    async def with_client(fn):
        need_telethon()
        row = get_auth()
        if not row:
            raise RuntimeError("Telegram is not connected. Open 'Telegram Setup' and log in first.")
        client = TelegramClient(StringSession(row["session_string"]), int(row["api_id"]), row["api_hash"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram session expired. Please disconnect and log in again.")
            return await fn(client)
        finally:
            await client.disconnect()

    def parse_tg_link(raw):
        """Returns dict(kind, value, msg_id) or None."""
        s = (raw or "").strip()
        m = re.search(r'(?:t|telegram)\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?', s)
        if m:
            nums = [x for x in m.groups()[1:] if x]
            return {"kind": "c", "value": m.group(1), "msg_id": int(nums[-1]) if nums else None}
        m = re.search(r'(?:t|telegram)\.me/(?:\+|joinchat/)([\w-]+)', s)
        if m:
            return {"kind": "invite", "value": m.group(1), "msg_id": None}
        m = re.search(r'(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,})(?:/(\d+))?', s)
        if m and m.group(1).lower() not in ("c", "joinchat", "s"):
            return {"kind": "user", "value": m.group(1), "msg_id": int(m.group(2)) if m.group(2) else None}
        m = re.fullmatch(r'@([A-Za-z][A-Za-z0-9_]{3,})', s)
        if m:
            return {"kind": "user", "value": m.group(1), "msg_id": None}
        return None

    async def find_entity_by_id(client, marked_id):
        async for d in client.iter_dialogs():
            if d.id == marked_id:
                return d.entity
        raise RuntimeError("This group/channel was not found in your Telegram account. Join it first, then try again.")

    async def resolve_entity(client, parsed):
        kind, value = parsed["kind"], parsed["value"]
        if kind == "c":
            return await find_entity_by_id(client, int("-100" + value))
        if kind == "user":
            return await client.get_entity(value)
        if kind == "invite":
            res = await client(functions.messages.CheckChatInviteRequest(value))
            chat = getattr(res, "chat", None)
            if chat is not None and res.__class__.__name__ in ("ChatInviteAlready", "ChatInvitePeek"):
                return chat
            raise RuntimeError("This account has not joined that private channel yet. Open the invite link in Telegram, tap Join, then try again.")
        raise RuntimeError("Unsupported Telegram link.")

    def entity_info(entity, dialog_id=None):
        username = getattr(entity, "username", None)
        is_channel_cls = entity.__class__.__name__ == "Channel"
        is_group = bool(getattr(entity, "megagroup", False)) or entity.__class__.__name__ == "Chat"
        is_channel = is_channel_cls and not is_group
        is_private = not username
        label = ("Private " if is_private else "Public ") + ("Group" if is_group else "Channel")
        ent_id = getattr(entity, "id", 0)
        if dialog_id is None:
            dialog_id = int("-100" + str(ent_id)) if is_channel_cls else -ent_id
        return {
            "id": dialog_id, "title": getattr(entity, "title", None) or username or str(ent_id),
            "username": username, "is_group": is_group, "is_channel": is_channel,
            "is_private": is_private, "type_label": label,
        }

    def source_url_for(entity):
        username = getattr(entity, "username", None)
        if username:
            return f"https://t.me/{username}"
        return f"https://t.me/c/{getattr(entity, 'id', '')}"

    def public_post_videos(username, msg_id):
        """No-login fallback for public channel posts via the embed page."""
        r = requests.get(f"https://t.me/{username}/{msg_id}?embed=1&mode=tme", timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        html = r.text
        text = re.sub(r"<br\s*/?>", "\n", html)
        hrefs = re.findall(r'href="(https?://[^"]+)"', html)
        return extract_videos_from_text(re.sub(r"<[^>]+>", " ", text), hrefs, f"Telegram: {username}")

    # ------------------------------------------------------------------ Telegram routes
    @app.route("/admin/api/telegram/status")
    def tg_status():
        g = admin_only()
        if g:
            return g
        row = get_auth()
        if not row:
            return jsonify({"success": True, "connected": False})
        return jsonify({
            "success": True, "connected": True, "user_name": row.get("user_name") or "Telegram user",
            "phone": str(row.get("phone") or "").lstrip("+"), "api_id": row.get("api_id"),
        })

    @app.route("/admin/api/telegram/send_code", methods=["POST"])
    def tg_send_code():
        g = admin_only()
        if g:
            return g
        data = request.get_json(silent=True) or {}
        phone = (data.get("phone") or "").strip()
        api_id = str(data.get("api_id") or os.environ.get("TELEGRAM_API_ID") or "").strip()
        api_hash = (data.get("api_hash") or os.environ.get("TELEGRAM_API_HASH") or "").strip()
        if not phone:
            return jerr("Phone number is required (with country code, e.g. +91...).")
        if not api_id.isdigit() or not api_hash:
            return jerr("API ID and API Hash are required. Get them from my.telegram.org, or set TELEGRAM_API_ID / TELEGRAM_API_HASH on the server.")
        if not phone.startswith("+"):
            phone = "+" + phone

        async def go():
            need_telethon()
            old = _login.get("client")
            if old:
                try:
                    await old.disconnect()
                except Exception:
                    pass
            client = TelegramClient(StringSession(), int(api_id), api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            _login.clear()
            _login.update({"client": client, "phone": phone, "hash": sent.phone_code_hash,
                           "api_id": int(api_id), "api_hash": api_hash, "need_pw": False})

        try:
            run_async(go())
            return jsonify({"success": True, "message": f"Login code sent to Telegram for {phone}."})
        except Exception as e:
            return jerr(e)

    @app.route("/admin/api/telegram/verify_code", methods=["POST"])
    def tg_verify_code():
        g = admin_only()
        if g:
            return g
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or "").strip()
        password = (data.get("password") or "").strip()
        if not _login.get("client"):
            return jerr("Login session expired. Click 'Send Login Code' again.")

        async def go():
            c = _login["client"]
            try:
                if _login.get("need_pw") and password:
                    await c.sign_in(password=password)
                else:
                    await c.sign_in(phone=_login["phone"], code=code, phone_code_hash=_login["hash"])
            except errors.SessionPasswordNeededError:
                _login["need_pw"] = True
                if not password:
                    return {"needs_2fa": True}
                await c.sign_in(password=password)
            me = await c.get_me()
            name = " ".join(x for x in [getattr(me, "first_name", ""), getattr(me, "last_name", "")] if x) or getattr(me, "username", "") or "Telegram user"
            session_string = c.session.save()
            await c.disconnect()
            return {"name": name, "session": session_string}

        try:
            res = run_async(go())
        except Exception as e:
            return jerr(e)
        if res.get("needs_2fa"):
            return jsonify({"success": False, "needs_2fa": True, "message": "This account has 2-step verification. Enter your Telegram password."})
        db("DELETE FROM telegram_auth")
        db("INSERT INTO telegram_auth (api_id, api_hash, session_string, phone, user_name, is_active) VALUES (?, ?, ?, ?, ?, 1)",
           (_login["api_id"], _login["api_hash"], res["session"], _login["phone"], res["name"]))
        _login.clear()
        return jsonify({"success": True, "message": f"Connected as {res['name']}."})

    @app.route("/admin/api/telegram/disconnect", methods=["POST"])
    def tg_disconnect():
        g = admin_only()
        if g:
            return g
        db("DELETE FROM telegram_auth")
        return jsonify({"success": True, "message": "Telegram disconnected."})

    @app.route("/admin/api/telegram/dialogs")
    def tg_dialogs():
        g = admin_only()
        if g:
            return g

        async def go(client):
            out = []
            async for d in client.iter_dialogs(limit=400):
                if d.is_group or d.is_channel:
                    out.append(entity_info(d.entity, d.id))
            return out

        try:
            return jsonify({"success": True, "dialogs": run_async(with_client(go))})
        except Exception as e:
            return jerr(e)

    @app.route("/admin/api/telegram/resolve_dialog", methods=["POST"])
    def tg_resolve_dialog():
        g = admin_only()
        if g:
            return g
        query = ((request.get_json(silent=True) or {}).get("query") or "").strip()
        parsed = parse_tg_link(query)
        if not parsed:
            return jerr("Paste a Telegram link like t.me/c/123456/7, t.me/+invite or t.me/username/12.")

        async def go(client):
            entity = await resolve_entity(client, parsed)
            info = entity_info(entity)
            info["msg_id"] = parsed["msg_id"]
            info["query"] = query
            return info

        try:
            return jsonify({"success": True, "dialog": run_async(with_client(go))})
        except Exception as e:
            return jerr(e)

    @app.route("/admin/api/telegram/add_source", methods=["POST"])
    def tg_add_source():
        g = admin_only()
        if g:
            return g
        data = request.get_json(silent=True) or {}
        gid = data.get("group_id")
        title = data.get("group_title") or "Telegram"

        async def go(client):
            return source_url_for(await find_entity_by_id(client, int(gid)))

        try:
            url = run_async(with_client(go))
            name = f"Telegram: {title}"
            if insert_ignore("feed_sources", ("name", "url", "is_active"), (name, url, 1)):
                return jsonify({"success": True, "message": f"'{title}' added as a monitored source."})
            return jsonify({"success": True, "message": f"'{title}' is already a source."})
        except Exception as e:
            return jerr(e)

    @app.route("/admin/api/telegram/import_dialog", methods=["POST"])
    def tg_import_dialog():
        g = admin_only()
        if g:
            return g
        data = request.get_json(silent=True) or {}
        gid = data.get("group_id")
        title = data.get("group_title") or "Telegram"
        limit = max(1, min(int(data.get("limit") or 30), 200))
        source_name = f"Telegram: {title}"

        async def go(client):
            entity = await find_entity_by_id(client, int(gid))
            src = db("SELECT id FROM feed_sources WHERE url = ?", (source_url_for(entity),), "one")
            new = 0
            async for msg in client.iter_messages(entity, limit=limit):
                for v in extract_videos_from_message(msg, source_name):
                    if add_video(v["title"], v["video_url"], v["surl"], src["id"] if src else None, source_name):
                        new += 1
            return new

        try:
            new = run_async(with_client(go), timeout=120)
        except Exception as e:
            return jerr(e)
        if new:
            return jsonify({"success": True, "message": f"Imported {new} new video(s) from {title}."})
        return jerr("No new videos found in this group.")

    # ------------------------------------------------------------------ feed video routes
    @app.route("/admin/api/feed/import_link", methods=["POST"])
    def feed_import_link():
        g = admin_only()
        if g:
            return g
        link = clean_link(((request.get_json(silent=True) or {}).get("link") or "").strip())
        if not link:
            return jerr("Please paste a link.")
        parsed = parse_tg_link(link) if "t.me" in link or "telegram.me" in link or link.startswith("@") else None
        videos = []
        try:
            if parsed:
                if not parsed["msg_id"]:
                    return jerr("This is a channel link, not a post link. Use 'Add Source' or open a specific post (t.me/.../123).")
                if get_auth():
                    async def go(client):
                        entity = await resolve_entity(client, parsed)
                        title = entity_info(entity)["title"]
                        msg = await client.get_messages(entity, ids=parsed["msg_id"])
                        return title, (extract_videos_from_message(msg, f"Telegram: {title}") if msg else [])
                    title, vids = run_async(with_client(go))
                    source_name = f"Telegram: {title}"
                elif parsed["kind"] == "user":
                    vids, source_name = public_post_videos(parsed["value"], parsed["msg_id"]), f"Telegram: {parsed['value']}"
                else:
                    return jerr("Connect Telegram first (Telegram Setup) to import from private channels.")
                for v in vids:
                    if add_video(v["title"], v["video_url"], v["surl"], None, source_name):
                        videos.append(v)
                if vids and not videos:
                    return jerr("This video is already in your feed.")
            else:
                if not link.lower().startswith("http"):
                    return jerr("Please paste a valid http(s) video link or a Telegram post link.")
                surl = extract_surl(link) or extract_youtube_id(link) or ""
                v = {"title": f"Video {surl}" if surl else "Video", "video_url": link, "surl": surl}
                if add_video(v["title"], link, surl, None, "Manual"):
                    videos.append(v)
                else:
                    return jerr("This video is already in your feed.")
        except Exception as e:
            return jerr(e)
        if not videos:
            return jerr("Could not find any video links in this post.")
        return jsonify({"success": True, "videos": videos, "message": f"Added {len(videos)} video(s) to the feed."})

    @app.route("/admin/api/feed/delete/<int:vid>", methods=["POST"])
    def feed_delete(vid):
        g = admin_only()
        if g:
            return g
        db("DELETE FROM feed_videos WHERE id = ?", (vid,))
        return jsonify({"success": True})

    @app.route("/admin/api/feed/clear", methods=["POST"])
    def feed_clear():
        g = admin_only()
        if g:
            return g
        db("DELETE FROM feed_videos")
        return jsonify({"success": True})

    # ------------------------------------------------------------------ source routes
    @app.route("/admin/api/sources/add", methods=["POST"])
    def sources_add():
        g = admin_only()
        if g:
            return g
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        if not url.lower().startswith("http"):
            return jerr("Please enter a valid URL starting with http(s)://")
        if not name:
            name = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        if not insert_ignore("feed_sources", ("name", "url", "is_active"), (name, url, 1)):
            return jerr("This source URL already exists.")
        return jsonify({"success": True, "message": f"Source '{name}' added."})

    @app.route("/admin/api/sources/toggle/<int:sid>", methods=["POST"])
    def sources_toggle(sid):
        g = admin_only()
        if g:
            return g
        row = db("SELECT is_active FROM feed_sources WHERE id = ?", (sid,), "one")
        if not row:
            return jerr("Source not found.", 404)
        new = 0 if row["is_active"] else 1
        db("UPDATE feed_sources SET is_active = ? WHERE id = ?", (new, sid))
        return jsonify({"success": True, "is_active": bool(new)})

    @app.route("/admin/api/sources/delete/<int:sid>", methods=["POST"])
    def sources_delete(sid):
        g = admin_only()
        if g:
            return g
        db("DELETE FROM feed_sources WHERE id = ?", (sid,))
        return jsonify({"success": True})

    # ------------------------------------------------------------------ sync engine
    state = {"is_syncing": False, "last_status": "Ready", "enabled": False,
             "interval_minutes": 10, "next_ts": None}
    sync_lock = threading.Lock()

    def sync_web_source(src):
        if not HAS_BS4:
            raise RuntimeError("beautifulsoup4 is not installed")
        r = requests.get(src["url"], timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        new, seen = 0, set()
        for a in soup.find_all("a", href=True):
            href = clean_link(a["href"])
            if not href.startswith("http") or href in seen or not is_video_link(href):
                continue
            seen.add(href)
            title = (a.get_text(" ", strip=True) or f"Video from {src['name']}")[:200]
            if add_video(title, href, extract_surl(href) or "", src["id"], src["name"]):
                new += 1
        return new

    async def sync_telegram_sources(sources):
        async def go(client):
            total = 0
            for src in sources:
                parsed = parse_tg_link(src["url"])
                if not parsed:
                    continue
                try:
                    entity = await resolve_entity(client, parsed)
                    async for msg in client.iter_messages(entity, limit=60):
                        for v in extract_videos_from_message(msg, src["name"]):
                            if add_video(v["title"], v["video_url"], v["surl"], src["id"], src["name"]):
                                total += 1
                    db("UPDATE feed_sources SET last_synced_at = CURRENT_TIMESTAMP WHERE id = ?", (src["id"],))
                except Exception as e:
                    print(f"Sync notice [{src['name']}]:", e)
                    state["last_status"] = f"{src['name']}: {e}"[:160]
            return total
        return await with_client(go)

    def sync_all():
        """Returns (new_count, message, ok)."""
        if not sync_lock.acquire(blocking=False):
            return 0, "A sync is already running.", False
        state["is_syncing"] = True
        try:
            sources = db("SELECT * FROM feed_sources WHERE is_active = 1", (), "all")
            if not sources:
                state["last_status"] = "No active sources"
                return 0, "No active sources to sync. Add or resume a source first.", False
            new, notes = 0, []
            tg = [s for s in sources if parse_tg_link(s["url"])]
            web = [s for s in sources if s not in tg]
            if tg:
                if get_auth():
                    try:
                        new += run_async(sync_telegram_sources(tg), timeout=180)
                    except Exception as e:
                        notes.append(f"Telegram: {e}")
                else:
                    notes.append("Telegram sources skipped (not connected)")
            for s in web:
                try:
                    new += sync_web_source(s)
                    db("UPDATE feed_sources SET last_synced_at = CURRENT_TIMESTAMP WHERE id = ?", (s["id"],))
                except Exception as e:
                    notes.append(f"{s['name']}: {e}")
            msg = f"Discovered {new} new video(s)." + (" " + " | ".join(notes) if notes else "")
            state["last_status"] = msg[:200]
            ok = not (notes and new == 0)
            return new, msg, ok
        finally:
            state["is_syncing"] = False
            sync_lock.release()

    @app.route("/admin/api/sources/sync", methods=["POST"])
    def sources_sync():
        g = admin_only()
        if g:
            return g
        try:
            new, msg, ok = sync_all()
        except Exception as e:
            return jerr(e)
        if not ok:
            return jerr(msg)
        return jsonify({"success": True, "new_count": new, "message": msg})

    # ------------------------------------------------------------------ auto sync (monitoring)
    def auto_payload():
        nxt = None
        if state["enabled"] and state["next_ts"]:
            nxt = max(0, int(state["next_ts"] - time.time()))
        return {"success": True, "enabled": state["enabled"], "interval_minutes": state["interval_minutes"],
                "next_sync_seconds": nxt, "is_syncing": state["is_syncing"], "last_status": state["last_status"]}

    def save_auto():
        set_setting("auto_sync", json.dumps({"enabled": state["enabled"], "interval_minutes": state["interval_minutes"]}))

    @app.route("/admin/api/sources/auto_sync", methods=["GET", "POST"])
    def sources_auto_sync():
        g = admin_only()
        if g:
            return g
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            if "interval_minutes" in data:
                try:
                    state["interval_minutes"] = max(1, min(int(data["interval_minutes"]), 1440))
                except (TypeError, ValueError):
                    return jerr("Invalid interval.")
            if "enabled" in data:
                state["enabled"] = bool(data["enabled"])
            if state["enabled"]:
                state["next_ts"] = time.time() + (3 if "enabled" in data else state["interval_minutes"] * 60)
                state["last_status"] = "Auto Sync active"
            else:
                state["next_ts"] = None
                state["last_status"] = "Paused"
            try:
                save_auto()
            except Exception as e:
                print("Auto sync save notice:", e)
        return jsonify(auto_payload())

    def auto_worker():
        while True:
            time.sleep(5)
            try:
                if state["enabled"] and state["next_ts"] and time.time() >= state["next_ts"] and not state["is_syncing"]:
                    state["next_ts"] = time.time() + state["interval_minutes"] * 60
                    sync_all()
            except Exception as e:
                print("Auto sync error:", e)
                state["last_status"] = f"Error: {e}"[:160]

    # ------------------------------------------------------------------ dashboard context
    def feed_context():
        """Template data for the admin dashboard feed section."""
        ctx = {"feed_sources": [], "feed_videos": [], "auto_sync": auto_payload(), "feed_stats": {}}
        try:
            sources = db("SELECT * FROM feed_sources ORDER BY id DESC", (), "all")
            counts = db("SELECT source_id, COUNT(*) AS c FROM feed_videos GROUP BY source_id", (), "all")
            cmap = {r["source_id"]: r["c"] for r in counts}
            for s in sources:
                s["video_count"] = cmap.get(s["id"], 0)
            ctx["feed_sources"] = sources
            ctx["feed_videos"] = db("SELECT * FROM feed_videos ORDER BY id DESC LIMIT 500", (), "all")
            ctx["feed_stats"] = {
                "total_feed_videos": sum(cmap.values()),
                "active_sources": sum(1 for s in sources if s["is_active"]),
            }
        except Exception as e:
            print("Feed context error:", e)
        return ctx

    # ------------------------------------------------------------------ startup
    try:
        ensure_tables()
        seed_default_sources()
        saved = json.loads(get_setting("auto_sync", "{}") or "{}")
        state["enabled"] = bool(saved.get("enabled"))
        state["interval_minutes"] = int(saved.get("interval_minutes") or 10)
        if state["enabled"]:
            state["next_ts"] = time.time() + 30
            state["last_status"] = "Auto Sync active"
    except Exception as e:
        print("Admin feed init notice:", e)

    threading.Thread(target=auto_worker, daemon=True).start()
    return feed_context
