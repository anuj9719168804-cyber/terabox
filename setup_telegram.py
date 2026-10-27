import os
import sys
import json
import asyncio
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

API_ID = int(os.environ.get("TELEGRAM_API_ID", 33029767))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "5d897bed11bc8b062a12f6c1c3c5360a")
SESSION_NAME = os.path.join(os.path.dirname(__file__), "telegram_session")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "telegram_config.json")

async def main():
    print("=" * 60)
    print("TeraStream Pro - Telegram Private Group Connection Setup")
    print("=" * 60)
    print(f"API ID:   {API_ID}")
    print(f"API Hash: {API_HASH[:8]}...{API_HASH[-4:]}")
    print("-" * 60)

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    first_name = getattr(me, 'first_name', '') or 'User'
    phone = getattr(me, 'phone', '') or 'Hidden'
    print(f"\n[OK] Successfully logged in as: {first_name} (+{phone})")

    print("\nFetching your Telegram dialogs (groups and channels)...")
    groups = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, (Channel, Chat)):
            groups.append({
                "id": dialog.id,
                "title": dialog.name or "Untitled",
                "is_channel": getattr(entity, 'broadcast', False),
                "is_group": getattr(entity, 'megagroup', False) or isinstance(entity, Chat)
            })

    if not groups:
        print("[!] No groups or channels found on this account.")
        await client.disconnect()
        return

    print("\nYour Groups & Channels:")
    print("-" * 60)
    for idx, g in enumerate(groups, 1):
        gtype = "Channel" if g["is_channel"] else "Group"
        print(f"[{idx:2d}] {g['title']} ({gtype}, ID: {g['id']})")
    print("-" * 60)

    choice = input("\nEnter the number of the private group to monitor: ").strip()
    try:
        selected_index = int(choice) - 1
        if selected_index < 0 or selected_index >= len(groups):
            print("[!] Invalid selection.")
            await client.disconnect()
            return
        selected_group = groups[selected_index]
    except ValueError:
        print("[!] Please enter a valid number.")
        await client.disconnect()
        return

    config = {
        "api_id": API_ID,
        "api_hash": API_HASH,
        "session_name": "telegram_session",
        "target_group_id": selected_group["id"],
        "target_group_title": selected_group["title"],
        "active": True
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

    print(f"\n[SUCCESS] Configuration saved!")
    print(f"Target Private Group: {selected_group['title']} (ID: {selected_group['id']})")
    # Register in feed_sources
    source_name = f"Telegram: {selected_group['title']}"
    source_url = f"https://t.me/c/{str(selected_group['id']).replace('-100', '')}"
    try:
        from app import get_db_connection, extract_surl
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute('''
                    INSERT INTO feed_sources (name, url, is_active)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (url) DO UPDATE SET is_active = TRUE
                ''', (source_name, source_url, True))
            else:
                cursor.execute('''
                    INSERT OR REPLACE INTO feed_sources (name, url, is_active)
                    VALUES (?, ?, ?)
                ''', (source_name, source_url, 1))
            conn.commit()
        conn.close()
    except Exception as e:
        print("Register source notice:", e)

    import re
    surl_pattern = re.compile(r'/s/(?:1)?([a-zA-Z0-9_-]{10,40})')

    new_links_count = 0
    async for msg in client.iter_messages(selected_group["id"], limit=200):
        text = msg.text or ""
        if not text and msg.media:
            text = getattr(msg, 'message', '') or ''

        links = re.findall(r'https?://[^\s]+', text)
        for link in links:
            if any(k in link.lower() for k in ['terabox', 'terashare', 'mirrobox', 'nephobox', '4funbox']):
                m = surl_pattern.search(link)
                surl = m.group(1) if m else ""
                lines = [l.strip() for l in text.split('\n') if l.strip() and not l.startswith('http')]
                title = lines[0] if lines else f"Telegram Video ({selected_group['title']})"
                try:
                    from app import get_db_connection
                    db_type, conn = get_db_connection()
                    with conn:
                        cursor = conn.cursor()
                        if db_type == "postgres":
                            cursor.execute('''
                                INSERT INTO feed_videos (source_id, source_name, title, video_url, thumbnail_url, surl)
                                VALUES (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT (video_url) DO NOTHING
                            ''', (selected_group['id'], source_name, title[:200], link, "", surl))
                        else:
                            cursor.execute('''
                                INSERT OR IGNORE INTO feed_videos (source_id, source_name, title, video_url, thumbnail_url, surl)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (selected_group['id'], source_name, title[:200], link, "", surl))
                        conn.commit()
                    conn.close()
                    new_links_count += 1
                except Exception:
                    pass

    print(f"[OK] Added {new_links_count} recent videos from this Telegram group to your Admin Feed!")
    print("\nTeraStream is now ready to receive new videos from this group in the Admin Panel.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
