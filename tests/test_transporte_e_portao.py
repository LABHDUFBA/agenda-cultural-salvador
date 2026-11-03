"""Regressões de transporte e do portão temporal (sem rede).

Cobrem três defeitos que não apareciam na suíte anterior porque nenhum teste
olhava o caminho HTTP nem a borda de fuso:

* ``starts_at`` é horário local de Salvador sem fuso; interpretá-lo como UTC
  bloqueava como "já ocorreu" um evento que ainda ia começar (3 h de adianto).
* ``HttpTransport.send_message`` truncava em 4096 caracteres: o fim de uma
  auditoria longa era descartado em silêncio em vez de ser enviado em partes.
* ``getUpdates`` pedia só ``allowed_updates=["message"]``, então
  ``callback_query`` nunca chegava ao handler — o ramo de botões inline era
  código morto.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.config import Settings
from bot.models import Event, EventStatus, UserRole
from bot.publisher import CONFIRMATION_PHRASE, PublishBlocked
from bot.runtime import build_runtime
from bot.telegram_api import (
    TELEGRAM_LIMIT,
    HttpTransport,
    IncomingUpdate,
    OutgoingMessage,
)

BAHIA = ZoneInfo("America/Bahia")
ADMIN = 8001
CHANNEL = -1005550001111


class FakeTransport:
    def __init__(self):
        self.messages = []
        self.events = []

    def send_message(self, message):
        self.messages.append(message)
        return f"msg-{len(self.messages)}"

    def send_event(self, event, chat_id):
        self.events.append((event.id, chat_id))
        return f"pub-{len(self.events)}"

    def get_updates(self, offset, timeout=25):
        return []


def settings_for(tmp_path, **overrides) -> Settings:
    base = dict(
        db_path=str(tmp_path / "agenda.db"),
        bot_token=None,
        target_chat_id=CHANNEL,
        allow_execute=False,
        max_per_run=3,
        min_interval_seconds=300,
        admin_ids=(ADMIN,),
        api_base="https://api.telegram.org",
    )
    base.update(overrides)
    return Settings(**base)


def local_now() -> datetime:
    return datetime.now(BAHIA).replace(second=0, microsecond=0)


def add_event(rt, starts_at: str, *, authorized: bool = True) -> Event:
    saved = rt.context.events.add(
        Event(
            title="Concerto da Orquestra",
            venue="Teatro Castro Alves",
            starts_at=starts_at,
            description="Apresentação com entrada gratuita.",
            source_url="https://exemplo.org/concerto",
            submitted_by=ADMIN,
            status=EventStatus.APPROVED,
            authorization_confirmed=authorized,
            authorization_reference="e-mail da produção 01/11/2026" if authorized else "",
        )
    )
    return saved


# ---------------------------------------------------------------------------
# portão temporal: horário local não é UTC
# ---------------------------------------------------------------------------
def test_evento_que_comeca_daqui_a_uma_hora_nao_e_tratado_como_passado(tmp_path):
    rt = build_runtime(settings_for(tmp_path), FakeTransport())
    try:
        event = add_event(rt, (local_now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
        assert rt.publisher.plan(event.id) == []
        text = rt.handlers.handle_update(
            IncomingUpdate(1, 555, ADMIN, "admin", f"/publicar {event.id}")
        )[0].text
        assert "DRY-RUN" in text and "ocorreu" not in text
    finally:
        rt.conn.close()


def test_evento_encerrado_continua_bloqueado(tmp_path):
    rt = build_runtime(settings_for(tmp_path), FakeTransport())
    try:
        event = add_event(rt, "2020-01-01T10:00")
        assert any("ocorreu" in b for b in rt.publisher.plan(event.id))
        with pytest.raises(PublishBlocked):
            rt.publisher.publish(event.id, ADMIN, dry_run=True)
    finally:
        rt.conn.close()


def test_evento_de_hoje_mais_tarde_publica_com_frase(tmp_path):
    rt = build_runtime(
        settings_for(tmp_path, allow_execute=True, min_interval_seconds=0), FakeTransport()
    )
    try:
        event = add_event(rt, (local_now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
        result = rt.publisher.publish(
            event.id, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE
        )
        assert result.outcome == "sent"
        assert rt.transport.events == [(event.id, CHANNEL)]
    finally:
        rt.conn.close()


# ---------------------------------------------------------------------------
# transporte HTTP (chamadas interceptadas: nenhum socket é aberto)
# ---------------------------------------------------------------------------
class RecordingTransport(HttpTransport):
    def __init__(self):
        super().__init__("token-falso")
        self.calls: list[tuple[str, dict]] = []

    def _call(self, method, payload):
        self.calls.append((method, payload))
        if method == "getUpdates":
            return self.queued_updates
        return {"message_id": len(self.calls)}

    queued_updates: list = []


def test_texto_longo_e_dividido_em_vez_de_truncado():
    transport = RecordingTransport()
    linhas = [f"linha {i:03d} " + "x" * 60 for i in range(200)]
    texto = "\n".join(linhas)
    assert len(texto) > TELEGRAM_LIMIT

    transport.send_message(OutgoingMessage(chat_id=1, text=texto))

    pedacos = [p["text"] for m, p in transport.calls if m == "sendMessage"]
    assert len(pedacos) > 1
    assert all(len(p) <= TELEGRAM_LIMIT for p in pedacos)
    # Nada foi perdido: o conteúdo remontado é o original.
    assert "".join(pedacos) == texto
    assert pedacos[-1].endswith(linhas[-1])


def test_linha_unica_maior_que_o_limite_nao_perde_conteudo():
    transport = RecordingTransport()
    texto = "z" * (TELEGRAM_LIMIT * 2 + 500)
    transport.send_message(OutgoingMessage(chat_id=1, text=texto))
    pedacos = [p["text"] for m, p in transport.calls if m == "sendMessage"]
    assert len(pedacos) == 3
    assert all(len(p) <= TELEGRAM_LIMIT for p in pedacos)
    assert "".join(pedacos) == texto


def test_teclado_inline_vai_no_ultimo_pedaco():
    transport = RecordingTransport()
    teclado = ((("Ver", "ver:7"),),)
    texto = "\n".join("y" * 60 for _ in range(120))
    transport.send_message(OutgoingMessage(chat_id=1, text=texto, keyboard=teclado))

    payloads = [p for m, p in transport.calls if m == "sendMessage"]
    assert len(payloads) > 1
    assert "reply_markup" not in payloads[0]
    assert payloads[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "ver:7"


def test_get_updates_pede_e_normaliza_callback_query():
    transport = RecordingTransport()
    transport.queued_updates = [
        {
            "update_id": 10,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 77, "username": "ana"},
                "data": "ver:42",
                "message": {"chat": {"id": -100999}, "message_id": 5},
            },
        },
        {
            "update_id": 11,
            "message": {"chat": {"id": -100999}, "from": {"id": 78}, "text": "/agenda"},
        },
        {"update_id": 12, "edited_message": {"chat": {"id": 1}}},
    ]

    updates = transport.get_updates(0)

    metodo, payload = transport.calls[0]
    assert metodo == "getUpdates"
    assert set(payload["allowed_updates"]) == {"message", "callback_query"}

    assert [u.update_id for u in updates] == [10, 11]
    cb, msg = updates
    assert cb.callback_data == "ver:42"
    assert cb.callback_id == "cb-1"
    assert cb.chat_id == -100999 and cb.user_id == 77 and cb.text == ""
    assert msg.text == "/agenda" and msg.callback_data is None


def test_callback_de_terceiro_nao_vira_comando():
    transport = RecordingTransport()
    transport.queued_updates = [
        {"update_id": 1, "callback_query": {"id": "cb", "from": {}, "data": "ver:1",
                                            "message": {"chat": {"id": 5}}}},
        {"update_id": 2, "callback_query": {"id": "cb2", "from": {"id": 9},
                                            "data": "", "message": {"chat": {"id": 5}}}},
    ]
    assert transport.get_updates(0) == []


# ---------------------------------------------------------------------------
# botões inline chegam ao handler e o callback é respondido
# ---------------------------------------------------------------------------
def test_callback_ver_devolve_detalhe_do_evento(tmp_path):
    rt = build_runtime(settings_for(tmp_path), FakeTransport())
    try:
        event = add_event(rt, (local_now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M"))
        out = rt.handlers.handle_update(
            IncomingUpdate(1, 555, ADMIN, "admin", "", callback_data=f"ver:{event.id}",
                           callback_id="cb-9")
        )
        assert f"#{event.id}" in out[0].text
    finally:
        rt.conn.close()


def test_roles_continuam_vindo_do_banco_para_callbacks(tmp_path):
    rt = build_runtime(settings_for(tmp_path), FakeTransport())
    try:
        event = add_event(rt, (local_now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M"))
        rt.context.users.upsert(999, "estranho", UserRole.PUBLIC)
        rt.context.events.update(
            Event(**{**rt.context.events.get(event.id).__dict__,
                     "status": EventStatus.PENDING})
        )
        out = rt.handlers.handle_update(
            IncomingUpdate(1, 555, 999, "estranho", "", callback_data=f"ver:{event.id}")
        )
        assert "não encontrado" in out[0].text.lower()
        assert "interno" not in out[0].text.lower()
    finally:
        rt.conn.close()


def test_mudar_papel_nao_apaga_o_username(tmp_path):
    rt = build_runtime(settings_for(tmp_path), FakeTransport())
    try:
        rt.context.users.upsert(4242, "produtora", UserRole.EDITOR)
        text = rt.handlers.handle_update(
            IncomingUpdate(1, 555, ADMIN, "admin", "/papel 4242 admin")
        )[0].text
        assert "admin" in text
        linhas = {tid: (nome, papel) for tid, nome, papel in rt.context.users.list_all()}
        assert linhas[4242] == ("produtora", UserRole.ADMIN)
    finally:
        rt.conn.close()
