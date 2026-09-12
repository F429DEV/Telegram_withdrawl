"""运行配置：全部来自环境变量 / .env 文件。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                out.add(int(chunk))
            except ValueError:
                continue
    return out


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: Path
    owner_ids: set[int] = field(default_factory=set)
    log_level: str = "INFO"
    max_active_per_chat: int = 5
    display_tz: str = "Asia/Shanghai"
    log_file: Optional[Path] = None
    log_max_mb: int = 10
    log_backups: int = 5
    ephemeral_seconds: int = 30

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(BASE_DIR / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "缺少 BOT_TOKEN。请复制 .env.example 为 .env，填入 @BotFather 给你的 token。"
            )
        db_raw = os.getenv("DB_PATH", "data/lottery.db").strip()
        db_path = Path(db_raw)
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return cls(
            bot_token=token,
            db_path=db_path,
            owner_ids=_parse_ids(os.getenv("OWNER_IDS", "")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            max_active_per_chat=int(os.getenv("MAX_ACTIVE_PER_CHAT", "5") or 5),
            display_tz=os.getenv("DISPLAY_TZ", "Asia/Shanghai").strip() or "Asia/Shanghai",
            log_file=cls._log_file(os.getenv("LOG_FILE", "logs/bot.log").strip()),
            log_max_mb=int(os.getenv("LOG_MAX_MB", "10") or 10),
            log_backups=int(os.getenv("LOG_BACKUPS", "5") or 5),
            ephemeral_seconds=int(os.getenv("EPHEMERAL_SECONDS", "30") or 0),
        )

    @staticmethod
    def _log_file(raw: str) -> Optional[Path]:
        """空字符串表示只往控制台打，不写文件。"""
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = BASE_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
