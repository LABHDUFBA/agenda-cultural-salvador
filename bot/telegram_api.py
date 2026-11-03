"""Telegram transport boundary.

Handlers never import HTTP libraries.  They emit ``OutgoingMessage`` objects and
the runtime decides how (or whether) to deliver them.  ``NullTransport`` is the
default so an unconfigured process cannot post anything, and ``HttpTransport``
is only constructed when a token is present *and* delivery is explicitly
enabled by configuration.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .models import Event

TELEGRAM_LIMIT = 4096


@dataclass(frozen=True)
class OutgoingMessage:
    chat_id: int
    text: str
    # Inline keyboards are plain data: list of rows of (label, callback_data).
    keyboard: tuple[tuple[tuple[str, str], ...], ...] = ()
    disable_notification: bool = True


@dataclass(frozen=True)
class IncomingUpdate:
    """Normalised subset of a Telegram update.  Ignore everything else."""

    update_id: int
    chat_id: int
    user_id: Optional[int]
    username: str
    text: str
    callback_data: Optional[str] = None
    # Presente só em callback_query; serve para fechar o "carregando" do botão.
    callback_id: Optional[str] = None


class Transport(Protocol):
    def send_message(self, message: OutgoingMessage) -> str: ...

    def send_event(self, event: Event, chat_id: int) -> str: ...

    def get_updates(self, offset: int, timeout: int = 25) -> list[IncomingUpdate]: ...


class NullTransport:
    """Safe default: records nothing on the wire, refuses to fabricate ids."""

    def __init__(self) -> None:
        self.delivered: list[OutgoingMessage] = []

    def send_message(self, message: OutgoingMessage) -> str:
        self.delivered.append(message)
        return f"null-{len(self.delivered)}"

    def send_event(self, event: Event, chat_id: int) -> str:
        raise RuntimeError("Envio desativado: nenhum transporte real configurado.")

    def get_updates(self, offset: int, timeout: int = 25) -> list[IncomingUpdate]:
        return []


class HttpTransport:
    """Minimal Bot API client.  Only used by the long-running entrypoint."""

    def __init__(self, token: str, api_base: str = "https://api.telegram.org", timeout: int = 20):
        if not token:
            raise ValueError("token ausente")
        self._api_base = api_base.rstrip("/")
        self._base = f"{self._api_base}/bot{token}"
        self._timeout = timeout
        # O token fica na URL da Bot API. Sem este __repr__, um log de objeto
        # (log.debug("%r", transport), inspeção em erro) imprimiria o segredo.
        self.__token_len = len(str(token))
        self._segredo = str(token)

    def __repr__(self) -> str:
        return f"HttpTransport(base={self._api_base!r}, token=<{self.__token_len} chars, oculto>)"

    __str__ = __repr__

    def _redigir(self, texto: object) -> str:
        """Remove o token de qualquer texto que vá para log/exceção."""
        return str(texto).replace(self._segredo, "<token>")

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            raise RuntimeError(f"Telegram HTTP {exc.code} em {method}") from None
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            # ``reason`` já não traz a URL, mas a redação garante que nenhuma
            # variação futura do urllib coloque o token no log.
            raise RuntimeError(f"Falha de rede em {method}: {self._redigir(exc.reason)}") from None
        if not body.get("ok"):
            raise RuntimeError(
                f"Telegram recusou {method}: {self._redigir(body.get('description', 'erro'))}"
            )
        return body.get("result")

    def send_message(self, message: OutgoingMessage) -> str:
        # A Bot API recusa texto acima de 4096 caracteres. Truncar descartava
        # o fim de listagens/auditorias em silêncio; aqui o texto é dividido em
        # linhas e o teclado vai no último pedaço.
        chunks = split_text(message.text, TELEGRAM_LIMIT)
        last_id = ""
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": message.chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
                "disable_notification": message.disable_notification,
            }
            if message.keyboard and index == len(chunks) - 1:
                payload["reply_markup"] = {
                    "inline_keyboard": [
                        [{"text": label, "callback_data": data} for label, data in row]
                        for row in message.keyboard
                    ]
                }
            result = self._call("sendMessage", payload)
            last_id = str(result["message_id"])
        return last_id

    def send_event(self, event: Event, chat_id: int) -> str:
        lines = [event.title, "", f"Local: {event.venue}", f"Quando: {event.starts_at}"]
        if event.ends_at:
            lines.append(f"até {event.ends_at}")
        lines += ["", event.description, "", f"Fonte: {event.source_url}"]
        return self.send_message(
            OutgoingMessage(chat_id=chat_id, text="\n".join(lines), disable_notification=False)
        )

    def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        """Fecha o indicador de carregamento do botão inline."""
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        self._call("answerCallbackQuery", payload)

    def get_updates(self, offset: int, timeout: int = 25) -> list[IncomingUpdate]:
        result = self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                # Sem "callback_query" aqui os botões inline nunca chegavam ao
                # handler: o ramo ``_callback`` existia mas era inalcançável.
                "allowed_updates": ["message", "callback_query"],
            },
        ) or []
        updates: list[IncomingUpdate] = []
        for raw in result:
            cb = raw.get("callback_query")
            if cb:
                msg = cb.get("message") or {}
                chat = msg.get("chat") or {}
                sender = cb.get("from") or {}
                data = cb.get("data")
                if not chat.get("id") or not data or not sender.get("id"):
                    continue
                updates.append(
                    IncomingUpdate(
                        update_id=int(raw.get("update_id", 0)),
                        chat_id=int(chat["id"]),
                        user_id=int(sender["id"]),
                        username=str(sender.get("username") or ""),
                        text="",
                        callback_data=str(data)[:64],
                        callback_id=str(cb.get("id") or "") or None,
                    )
                )
                continue
            msg = raw.get("message") or {}
            chat = msg.get("chat") or {}
            sender = msg.get("from") or {}
            if not chat.get("id") or "text" not in msg:
                continue
            updates.append(
                IncomingUpdate(
                    update_id=int(raw.get("update_id", 0)),
                    chat_id=int(chat["id"]),
                    user_id=int(sender["id"]) if sender.get("id") else None,
                    username=str(sender.get("username") or ""),
                    text=str(msg.get("text") or ""),
                )
            )
        return updates


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so a long listing is never truncated."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        # Uma única linha maior que o limite era cortada em ``line[:limit]`` e o
        # resto desaparecia. Agora ela é fatiada em pedaços completos.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]
