import asyncio
import os
import re
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any, Callable
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    FileReferenceExpiredError,
    ChannelPrivateError,
    ChatAdminRequiredError,
)
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
)

from app.config import settings, DEVICE_PROFILES
from app.session_manager import get_saved_session_string


class DownloadedMedia:
    def __init__(self, path: Path, caption: Optional[str] = None):
        self.path = path
        self.caption = caption or ""
        self.size = path.stat().st_size if path.exists() else 0


class UserbotService:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.me: Optional[Any] = None

    async def start(self) -> bool:
        session_str = get_saved_session_string()
        if not session_str or not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
            return False

        profile = DEVICE_PROFILES[0]
        self.client = TelegramClient(
            StringSession(session_str),
            api_id=settings.TELEGRAM_API_ID,
            api_hash=settings.TELEGRAM_API_HASH,
            device_model=profile["device_model"],
            system_version=profile["system_version"],
            app_version=profile["app_version"],
            lang_code=profile["lang_code"],
            system_lang_code=profile["system_lang_code"],
        )

        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            self.client = None
            return False

        self.me = await self.client.get_me()
        return True

    async def stop(self) -> None:
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self.client = None
        self.me = None

    def is_ready(self) -> bool:
        return self.client is not None and self.client.is_connected() and self.me is not None

    @staticmethod
    def parse_post_link(link: str) -> Optional[Tuple[Any, int]]:
        cleaned = link.strip().split("?")[0].rstrip("/")
        # Private channel: t.me/c/123456789/42
        m_priv = re.search(r"t\.me/c/(\d+)/(\d+)", cleaned)
        if m_priv:
            channel_id = int("-100" + m_priv.group(1))
            post_id = int(m_priv.group(2))
            return channel_id, post_id

        # Public channel: t.me/channel_name/42
        m_pub = re.search(r"t\.me/([a-zA-Z0-9_]+)/(\d+)", cleaned)
        if m_pub:
            username = m_pub.group(1)
            post_id = int(m_pub.group(2))
            return username, post_id

        return None

    async def fetch_and_download(
        self,
        link: str,
        progress_callback: Optional[Callable[[int, int, str], Any]] = None,
    ) -> List[DownloadedMedia]:
        if not self.is_ready():
            raise RuntimeError("userbot is not authorized or connected")

        parsed = self.parse_post_link(link)
        if not parsed:
            raise ValueError("invalid telegram post link")

        peer, post_id = parsed
        try:
            entity = await self.client.get_entity(peer)
        except (ChannelPrivateError, ChatAdminRequiredError):
            raise PermissionError("userbot has no access to this private channel; join it first")

        target_message = await self.client.get_messages(entity, ids=post_id)
        if not target_message or not target_message.media:
            raise ValueError("post not found or does not contain media")

        messages = [target_message]
        if target_message.grouped_id:
            # Album detection: query surrounding messages to capture the whole media group
            start_id = max(1, post_id - 10)
            end_id = post_id + 10
            surrounding = await self.client.get_messages(entity, ids=list(range(start_id, end_id + 1)))
            album_msgs = [
                m for m in surrounding
                if m and m.grouped_id == target_message.grouped_id and m.media
            ]
            if album_msgs:
                album_msgs.sort(key=lambda x: x.id)
                messages = album_msgs

        results: List[DownloadedMedia] = []
        total_items = len(messages)

        for idx, msg in enumerate(messages, start=1):
            for attempt in range(2):
                try:
                    def _prog(received: int, total: int):
                        if progress_callback and total > 0:
                            progress_callback(received, total, f"Загрузка {idx}/{total_items}")

                    dest_dir = settings.DOWNLOAD_DIR / f"task_{post_id}"
                    dest_dir.mkdir(parents=True, exist_ok=True)

                    downloaded_path = await self.client.download_media(
                        msg,
                        file=str(dest_dir) + "/",
                        progress_callback=_prog,
                    )
                    if downloaded_path:
                        media_file = Path(downloaded_path)
                        results.append(DownloadedMedia(path=media_file, caption=msg.text or ""))
                    break
                except FileReferenceExpiredError:
                    msg = await self.client.get_messages(entity, ids=msg.id)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)

        return results

    async def send_media_files(
        self,
        target_peer: Any,
        media_list: List[DownloadedMedia],
        progress_callback: Optional[Callable[[int, int, str], Any]] = None,
    ) -> None:
        if not self.is_ready():
            raise RuntimeError("userbot is not authorized or connected")

        total = len(media_list)
        for idx, item in enumerate(media_list, start=1):
            def _prog(sent: int, total_bytes: int):
                if progress_callback and total_bytes > 0:
                    progress_callback(sent, total_bytes, f"Отправка {idx}/{total}")

            for _ in range(2):
                try:
                    await self.client.send_file(
                        target_peer,
                        file=str(item.path),
                        caption=item.caption or None,
                        progress_callback=_prog,
                    )
                    break
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)


userbot_service = UserbotService()
