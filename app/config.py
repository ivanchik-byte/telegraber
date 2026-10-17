import json
from pathlib import Path
from typing import Optional, Literal, Any
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

FallbackDestination = Literal["direct", "channel"]

DEVICE_PROFILES = [
    {
        "device_model": "Desktop",
        "system_version": "Windows 11 Pro 64-bit",
        "app_version": "5.2.0 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "PC 64bit",
        "system_version": "Windows 10 Pro 64-bit",
        "app_version": "5.1.7 x64",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
    {
        "device_model": "MacBook Pro",
        "system_version": "macOS 14.4",
        "app_version": "10.9.1",
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    },
]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BOT_TOKEN: str = ""
    OWNER_ID: int = 0
    TELEGRAM_API_ID: int = 2040
    TELEGRAM_API_HASH: str = "b18441a1b366a10c4822d4f3521e4b24"
    FALLBACK_DESTINATION: FallbackDestination = "direct"
    TARGET_CHAT_ID: Optional[int] = None

    DOWNLOAD_DIR: Path = Path("/app/downloads")
    SESSIONS_DIR: Path = Path("/app/sessions")
    TDATA_DIR: Path = Path("/app/tdata_input")

    @field_validator("BOT_TOKEN", mode="before")
    @classmethod
    def clean_token(cls, v: Any) -> str:
        return str(v).strip() if v else ""

    @field_validator("OWNER_ID", mode="before")
    @classmethod
    def clean_owner_id(cls, v: Any) -> int:
        if not v or (isinstance(v, str) and not v.strip()):
            return 0
        return int(v)

    @field_validator("TELEGRAM_API_ID", mode="before")
    @classmethod
    def clean_api_id(cls, v: Any) -> int:
        if not v or (isinstance(v, str) and not v.strip()):
            return 2040
        return int(v)

    @field_validator("TELEGRAM_API_HASH", mode="before")
    @classmethod
    def clean_api_hash(cls, v: Any) -> str:
        if not v or (isinstance(v, str) and not v.strip()):
            return "b18441a1b366a10c4822d4f3521e4b24"
        return str(v).strip()

    @field_validator("TARGET_CHAT_ID", mode="before")
    @classmethod
    def clean_target_chat(cls, v: Any) -> Optional[int]:
        if not v or (isinstance(v, str) and not v.strip()):
            return None
        return int(v)

    def get_runtime_config_path(self) -> Path:
        return self.SESSIONS_DIR / "runtime_config.json"

    def load_runtime_overrides(self) -> None:
        cfg_file = self.get_runtime_config_path()
        if not cfg_file.exists():
            return
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "FALLBACK_DESTINATION" in data and data["FALLBACK_DESTINATION"] in ("direct", "channel"):
                    self.FALLBACK_DESTINATION = data["FALLBACK_DESTINATION"]
                if "TARGET_CHAT_ID" in data:
                    self.TARGET_CHAT_ID = data["TARGET_CHAT_ID"]
                if "TELEGRAM_API_ID" in data and data["TELEGRAM_API_ID"]:
                    self.TELEGRAM_API_ID = int(data["TELEGRAM_API_ID"])
                if "TELEGRAM_API_HASH" in data and data["TELEGRAM_API_HASH"]:
                    self.TELEGRAM_API_HASH = str(data["TELEGRAM_API_HASH"])
        except Exception:
            pass

    def save_runtime_overrides(self) -> None:
        self.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "FALLBACK_DESTINATION": self.FALLBACK_DESTINATION,
            "TARGET_CHAT_ID": self.TARGET_CHAT_ID,
            "TELEGRAM_API_ID": self.TELEGRAM_API_ID,
            "TELEGRAM_API_HASH": self.TELEGRAM_API_HASH,
        }
        with open(self.get_runtime_config_path(), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

settings = Settings()
settings.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
settings.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
settings.TDATA_DIR.mkdir(parents=True, exist_ok=True)
settings.load_runtime_overrides()
