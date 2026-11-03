"""Configuration from the environment.  Safe by default: nothing is enabled."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .publisher import PublisherConfig

DEFAULT_DB = "data/agenda.db"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "sim")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: str
    bot_token: Optional[str]
    target_chat_id: Optional[int]
    allow_execute: bool
    max_per_run: int
    min_interval_seconds: int
    admin_ids: tuple[int, ...]
    api_base: str

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Settings":
        env = env if env is not None else os.environ
        token = (env.get("BOT_TOKEN") or "").strip() or None
        chat_raw = (env.get("TARGET_CHAT_ID") or "").strip()
        target = int(chat_raw) if chat_raw.lstrip("-").isdigit() else None
        # Chat 0 não é endereço válido na Bot API; tratá-lo como "configurado"
        # deixaria o portão aberto com um destino que nunca recebe nada.
        if target == 0:
            target = None
        admins: list[int] = []
        for part in (env.get("ADMIN_IDS") or "").replace(";", ",").split(","):
            part = part.strip()
            # Ids do Telegram são positivos; 0/negativo aqui é erro de
            # digitação e não deve virar operador.
            if part.isdigit() and int(part) > 0:
                admins.append(int(part))
        api_base = (env.get("TELEGRAM_API_BASE") or "").strip() or "https://api.telegram.org"
        if not api_base.startswith(("https://", "http://")):
            raise ValueError(
                "TELEGRAM_API_BASE deve começar com https:// (ou http:// em rede local)"
            )
        return cls(
            db_path=env.get("AGENDA_DB") or DEFAULT_DB,
            bot_token=token,
            target_chat_id=target,
            # Execução real exige a flag E um token/chave de confirmação por comando.
            allow_execute=_flag("AGENDA_ALLOW_EXECUTE"),
            max_per_run=max(1, min(_int("AGENDA_MAX_PER_RUN", 3), 10)),
            min_interval_seconds=max(0, _int("AGENDA_MIN_INTERVAL", 300)),
            admin_ids=tuple(admins),
            api_base=api_base.rstrip("/"),
        )

    @property
    def db_file(self) -> Path:
        return Path(self.db_path)

    def publisher_config(self) -> PublisherConfig:
        return PublisherConfig(
            target_chat_id=self.target_chat_id,
            allow_execute=self.allow_execute,
            max_per_run=self.max_per_run,
            min_interval_seconds=self.min_interval_seconds,
        )
