import asyncio
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

from app.config import settings, DEVICE_PROFILES

SESSION_FILE_PATH = settings.SESSIONS_DIR / "userbot.session"
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_TOTAL_BYTES = 100 * 1024 * 1024


def get_default_device_profile() -> Dict[str, str]:
    return DEVICE_PROFILES[0]


def get_saved_session_string() -> Optional[str]:
    if SESSION_FILE_PATH.exists():
        raw = SESSION_FILE_PATH.read_text(encoding="utf-8").strip()
        return raw or None
    return None


def save_session_string(session_str: str) -> None:
    settings.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE_PATH.write_text(session_str.strip(), encoding="utf-8")


def delete_session() -> None:
    if SESSION_FILE_PATH.exists():
        SESSION_FILE_PATH.unlink()


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    total_bytes = 0
    with zipfile.ZipFile(zip_path, "r") as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise ValueError("archive has too many files")
        for entry in entries:
            total_bytes += entry.file_size
            if total_bytes > MAX_ZIP_TOTAL_BYTES:
                raise ValueError("archive exceeds maximum uncompressed size")
            name = entry.filename
            if name.startswith("/") or name.startswith("\\") or ".." in name:
                raise ValueError("unsafe path in archive")
            target = (dest_dir / name).resolve()
            if not str(target).startswith(str(dest_dir.resolve())):
                raise ValueError("path traversal detected")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


async def convert_tdata_from_path(
    source_path: Path,
    password: Optional[str] = None
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    try:
        from opentele2.td import TDesktop
        from opentele2.api import CreateNewSession
    except ImportError:
        return False, "opentele2 library not available", None

    temp_dir = Path(tempfile.mkdtemp(prefix="tdata_conv_"))
    try:
        target_dir: Optional[Path] = None
        if source_path.is_file() and source_path.suffix.lower() == ".zip":
            await asyncio.to_thread(_safe_extract_zip, source_path, temp_dir)
            for root, dirs, files in os.walk(temp_dir):
                if "key_data" in files or "key_datas" in files:
                    target_dir = Path(root)
                    break
            if not target_dir:
                for root, dirs, _ in os.walk(temp_dir):
                    for d in dirs:
                        if d.lower() == "tdata":
                            target_dir = Path(root) / d
                            break
                    if target_dir:
                        break
            if not target_dir:
                target_dir = temp_dir
        elif source_path.is_dir():
            target_dir = source_path
        else:
            return False, "unsupported tdata source", None

        tdesk = await asyncio.to_thread(TDesktop, str(target_dir))
        if not tdesk.isLoaded() or not tdesk.accounts:
            return False, "tdata does not contain valid accounts", None

        telethon_client: TelegramClient = await tdesk.ToTelethon(
            flag=CreateNewSession,
            password=password,
        )

        try:
            if not telethon_client.is_connected():
                await telethon_client.connect()
            if not await telethon_client.is_user_authorized():
                return False, "failed to authorize session from tdata", None

            me = await telethon_client.get_me()
            session_str = StringSession.save(telethon_client.session)
            save_session_string(session_str)

            account_data = {
                "id": me.id,
                "first_name": me.first_name,
                "last_name": me.last_name,
                "username": me.username,
                "phone": getattr(me, "phone", None),
            }
            return True, "ok", account_data
        finally:
            await telethon_client.disconnect()

    except SessionPasswordNeededError:
        return False, "2fa_required", None
    except Exception as exc:
        err_msg = str(exc)
        if "password" in err_msg.lower():
            return False, "2fa_required", None
        return False, err_msg, None
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


async def check_tdata_directory(password: Optional[str] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    if not settings.TDATA_DIR.exists():
        return False, "tdata directory does not exist", None

    candidates = list(settings.TDATA_DIR.glob("*.zip"))
    if not candidates:
        subdirs = [p for p in settings.TDATA_DIR.iterdir() if p.is_dir()]
        if subdirs:
            candidates = subdirs

    if not candidates:
        return False, "no tdata files found", None

    # Pick the most recently modified tdata
    target = max(candidates, key=lambda p: p.stat().st_mtime)
    return await convert_tdata_from_path(target, password=password)
