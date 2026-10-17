import asyncio
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Optional, List, Tuple, Any, Callable

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    FileReferenceExpiredError,
    ChannelPrivateError,
    ChatAdminRequiredError,
)

from app.config import settings, DEVICE_PROFILES
from app.session_manager import get_saved_session_string

logger = logging.getLogger(__name__)


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
        if not session_str:
            return False

        api_id, api_hash = settings.get_api_credentials()
        profile = DEVICE_PROFILES[0]
        self.client = TelegramClient(
            StringSession(session_str),
            api_id=api_id,
            api_hash=api_hash,
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
        m_priv = re.search(r"(?:t|telegram)\.me/c/(\d+)(?:/\d+)?/(\d+)$", cleaned)
        if m_priv:
            channel_id = int("-100" + m_priv.group(1))
            post_id = int(m_priv.group(2))
            return channel_id, post_id

        m_pub = re.search(r"(?:t|telegram)\.me/([a-zA-Z0-9_]+)(?:/\d+)?/(\d+)$", cleaned)
        if m_pub and m_pub.group(1) != "c":
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
            logger.info("Target post %s has grouped_id=%s. Searching album...", post_id, target_message.grouped_id)
            album_msgs_map = {target_message.id: target_message}

            # Telegram albums contain 2 to 10 items sharing the same grouped_id.
            # In channels with concurrent uploads or forwarded bundles, IDs can be interleaved.
            start_id = max(1, post_id - 50)
            end_id = post_id + 50
            ids_to_fetch = list(range(start_id, end_id + 1))
            surrounding = await self.client.get_messages(entity, ids=ids_to_fetch)
            for m in surrounding:
                if m and m.grouped_id == target_message.grouped_id and m.media:
                    album_msgs_map[m.id] = m

            # If still fewer than 10 items, check if album items were near the search boundaries
            if len(album_msgs_map) < 10:
                min_found = min(album_msgs_map.keys())
                max_found = max(album_msgs_map.keys())
                extra_ids = []
                if min_found <= start_id + 5 and start_id > 1:
                    extra_ids.extend(range(max(1, start_id - 50), start_id))
                if max_found >= end_id - 5:
                    extra_ids.extend(range(end_id + 1, end_id + 51))
                if extra_ids:
                    extra_msgs = await self.client.get_messages(entity, ids=extra_ids)
                    for m in extra_msgs:
                        if m and m.grouped_id == target_message.grouped_id and m.media:
                            album_msgs_map[m.id] = m

            messages = sorted(album_msgs_map.values(), key=lambda x: x.id)
            logger.info(
                "Found %d media items for album grouped_id=%s: IDs=%s",
                len(messages),
                target_message.grouped_id,
                [m.id for m in messages],
            )

        clean_peer = re.sub(r"[^a-zA-Z0-9_]", "", str(peer))
        dest_dir = settings.DOWNLOAD_DIR / f"task_{clean_peer}_{post_id}_{int(time.time() * 1000)}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        results: List[DownloadedMedia] = []
        total_items = len(messages)
        max_attempts = 3

        try:
            for idx, msg in enumerate(messages, start=1):
                downloaded = False
                for attempt in range(1, max_attempts + 1):
                    try:
                        def _prog(received: int, total: int):
                            if progress_callback and total > 0:
                                progress_callback(received, total, f"Загрузка {idx}/{total_items}")

                        downloaded_path = await self.client.download_media(
                            msg,
                            file=str(dest_dir) + "/",
                            progress_callback=_prog,
                        )
                        if downloaded_path:
                            media_file = Path(downloaded_path)
                            if media_file.name.startswith("."):
                                fixed_name = f"media_{msg.id}{media_file.name}"
                                fixed_path = media_file.parent / fixed_name
                                media_file.rename(fixed_path)
                                media_file = fixed_path
                            elif not media_file.suffix:
                                if hasattr(msg, "video") and msg.video:
                                    fixed_path = media_file.with_suffix(".mp4")
                                    media_file.rename(fixed_path)
                                    media_file = fixed_path
                                elif hasattr(msg, "photo") and msg.photo:
                                    fixed_path = media_file.with_suffix(".jpg")
                                    media_file.rename(fixed_path)
                                    media_file = fixed_path
                            results.append(DownloadedMedia(path=media_file, caption=msg.text or ""))
                        downloaded = True
                        break
                    except FileReferenceExpiredError:
                        logger.warning("File reference expired for msg %s, refetching (attempt %d/%d)", msg.id, attempt, max_attempts)
                        refetched = await self.client.get_messages(entity, ids=msg.id)
                        if refetched:
                            msg = refetched
                    except FloodWaitError as e:
                        logger.warning("FloodWait %d seconds while downloading msg %s (attempt %d/%d)", e.seconds, msg.id, attempt, max_attempts)
                        if attempt == max_attempts:
                            raise
                        await asyncio.sleep(e.seconds)

                if not downloaded:
                    raise RuntimeError(f"Failed to download media for message {msg.id} after {max_attempts} attempts")

            return results
        except Exception:
            if dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
            raise

    async def send_media_files(
        self,
        target_peer: Any,
        media_list: List[DownloadedMedia],
        progress_callback: Optional[Callable[[int, int, str], Any]] = None,
        as_album: bool = True,
    ) -> None:
        if not self.is_ready():
            raise RuntimeError("userbot is not authorized or connected")

        total = len(media_list)
        max_attempts = 3

        if total == 1 or not as_album:
            for idx, item in enumerate(media_list, start=1):
                def _prog(sent: int, total_bytes: int):
                    if progress_callback and total_bytes > 0:
                        progress_callback(sent, total_bytes, f"Отправка {idx}/{total}")

                sent = False
                for attempt in range(1, max_attempts + 1):
                    try:
                        await self.client.send_file(
                            target_peer,
                            file=str(item.path),
                            caption=item.caption or None,
                            progress_callback=_prog,
                        )
                        sent = True
                        break
                    except FloodWaitError as e:
                        logger.warning("FloodWait %d seconds while sending file %s (attempt %d/%d)", e.seconds, item.path.name, attempt, max_attempts)
                        if attempt == max_attempts:
                            raise
                        await asyncio.sleep(e.seconds)
                if not sent:
                    raise RuntimeError(f"Failed to send file {item.path.name} after {max_attempts} attempts")
            return

        for i in range(0, total, 10):
            chunk = media_list[i : i + 10]
            chunk_files = [str(x.path) for x in chunk]
            chunk_captions = [x.caption or "" for x in chunk]
            sent = False
            for attempt in range(1, max_attempts + 1):
                try:
                    if len(chunk_files) == 1:
                        await self.client.send_file(
                            target_peer,
                            file=chunk_files[0],
                            caption=chunk_captions[0] or None,
                        )
                    else:
                        await self.client.send_file(
                            target_peer,
                            file=chunk_files,
                            caption=chunk_captions,
                        )
                    sent = True
                    break
                except FloodWaitError as e:
                    logger.warning("FloodWait %d seconds while sending album chunk (attempt %d/%d)", e.seconds, attempt, max_attempts)
                    if attempt == max_attempts:
                        raise
                    await asyncio.sleep(e.seconds)
            if not sent:
                raise RuntimeError(f"Failed to send album chunk after {max_attempts} attempts")


userbot_service = UserbotService()
