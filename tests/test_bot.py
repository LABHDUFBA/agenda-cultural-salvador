"""End-to-end-ish tests: SQLite + handlers + publisher, all offline.

There is no network stack in the test path: ``FakeTransport`` records calls and
``NullTransport`` is the production default, so a passing suite proves the bot
cannot publish unless every gate is opened deliberately.
"""

from __future__ import annotations

import itertools
import re
import sqlite3

import pytest

from bot import admin as admin_cli
from bot.config import Settings
from bot.db import (
    DuplicateEvent,
    SqliteAuditRepository,
    SqliteEventRepository,
    SqlitePublishAttemptRepository,
    SqliteUserRepository,
    connect,
    init_schema,
)
from bot.handlers import BotHandlers, Throttle
from bot.models import (
    Event,
    EventStatus,
    NotFound,
    PermissionDenied,
    PublishBlocked,
    UserRole,
    ValidationError,
)
from bot.publisher import CONFIRMATION_PHRASE, Publisher, PublisherConfig
from bot.permissions import PermissionService
from bot.runtime import build_runtime
from bot.telegram_api import IncomingUpdate, NullTransport

ADMIN = 9001
EDITOR = 9002
STRANGER = 9003
CHANNEL = -1001234567890
FUTURE = "2027-03-14T19:00"


class FakeTransport:
    """Records outbound calls; never leaves the process."""

    def __init__(self, fail: bool = False):
        self.messages: list = []
        self.events: list[tuple[int, int]] = []
        self.fail = fail

    def send_message(self, message):
        self.messages.append(message)
        return f"msg-{len(self.messages)}"

    def send_event(self, event, chat_id):
        if self.fail:
            raise RuntimeError("rede indisponível")
        self.events.append((event.id, chat_id))
        return f"pub-{len(self.events)}"

    def get_updates(self, offset, timeout=25):
        return []


def make_settings(tmp_path, **overrides) -> Settings:
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


@pytest.fixture
def runtime(tmp_path):
    settings = make_settings(tmp_path)
    rt = build_runtime(settings, FakeTransport())
    rt.context.users.upsert(EDITOR, "editor", UserRole.EDITOR)
    return rt


def update(text: str, user_id: int = STRANGER, chat_id: int = 555) -> IncomingUpdate:
    return IncomingUpdate(update_id=1, chat_id=chat_id, user_id=user_id,
                          username="someone", text=text)


def say(runtime, text: str, user_id: int = STRANGER) -> str:
    out = runtime.handlers.handle_update(update(text, user_id))
    assert out, "handler must always answer"
    return "\n".join(m.text for m in out)


_seq = itertools.count(1)


def new_event(runtime, *, status=EventStatus.PENDING, authorized=False,
              source="https://exemplo.org/evento", title=None):
    event = Event(
        title=title or f"Show de Teste {next(_seq)}",
        venue="Teatro Castro Alves",
        starts_at=FUTURE,
        description="Apresentação de teste da agenda cultural.",
        source_url=source,
        submitted_by=EDITOR,
        status=status,
        authorization_confirmed=authorized,
        authorization_reference="e-mail da produtora 01/11/2026" if authorized else "",
    )
    return runtime.context.events.add(event)


def approved_event(runtime, **kwargs):
    event = new_event(runtime, **kwargs)
    if event.status is EventStatus.PENDING:
        runtime.context.events.update(
            Event(**{**event.__dict__, "status": EventStatus.APPROVED})
        )
    return runtime.context.events.get(event.id)


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def test_schema_and_repository_roundtrip(tmp_path):
    conn = connect(tmp_path / "x.db")
    init_schema(conn)
    events = SqliteEventRepository(conn)

    saved = events.add(Event(title="Feira", venue="Pelourinho", starts_at="2027-01-05T10:00",
                             description="Feira de artesanato", source_url="https://a.b/c",
                             submitted_by=EDITOR))
    assert saved.id == 1
    assert saved.status is EventStatus.PENDING

    fetched = events.get(saved.id)
    assert fetched.title == "Feira"
    assert events.count_by_status() == {"pending_review": 1}


def test_duplicate_event_is_refused(runtime):
    new_event(runtime, title="Mesmo Evento")
    with pytest.raises(DuplicateEvent):
        new_event(runtime, title="Mesmo Evento")


def test_validation_rejects_bad_input(runtime):
    with pytest.raises(ValidationError):
        runtime.context.events.add(Event(title="", venue="X", starts_at=FUTURE,
                                        submitted_by=EDITOR))
    with pytest.raises(ValidationError):
        runtime.context.events.add(Event(title="A", venue="X", starts_at="amanhã",
                                         submitted_by=EDITOR))
    with pytest.raises(ValidationError):
        runtime.context.events.add(Event(title="A", venue="X", starts_at=FUTURE,
                                         source_url="javascript:alert(1)",
                                         submitted_by=EDITOR))


# --------------------------------------------------------------------------
# public consultation
# --------------------------------------------------------------------------
def test_agenda_lists_only_public_events(runtime):
    pending = new_event(runtime)
    approved = approved_event(runtime)
    text = say(runtime, "/agenda", user_id=STRANGER)
    assert approved.title in text
    assert f"#{pending.id} " not in text


def test_public_cannot_see_pending_event_detail(runtime):
    pending = new_event(runtime)
    assert "não encontrado" in say(runtime, f"/evento {pending.id}", STRANGER)


def test_approved_detail_hides_authorization_reference(runtime):
    event = approved_event(runtime, authorized=True)
    text = say(runtime, f"/evento {event.id}", STRANGER)
    assert "e-mail da produtora" not in text
    assert "Autorização" not in text
    # editors get the internal view
    internal = say(runtime, f"/evento {event.id}", EDITOR)
    assert "e-mail da produtora" in internal


def test_search_is_accent_insensitive(runtime):
    approved_event(runtime)
    text = say(runtime, "/buscar teatro", STRANGER)
    assert "Show de Teste" in text
    assert "nada encontrado" in say(runtime, "/buscar jazz", STRANGER).lower()


def test_unknown_command_and_non_command(runtime):
    assert "desconhecido" in say(runtime, "/hackear")
    assert "/ajuda" in say(runtime, "olá")


# --------------------------------------------------------------------------
# permissions (server-side)
# --------------------------------------------------------------------------
def test_non_admin_cannot_approve(runtime):
    event = new_event(runtime, authorized=True)
    text = say(runtime, f"/aprovar {event.id}", EDITOR)
    assert "restrita" in text.lower()
    assert runtime.context.events.get(event.id).status is EventStatus.PENDING
    denials = [e for e in runtime.context.audit.recent() if e.outcome == "denied"]
    assert denials and denials[0].action == "event.approve"


def test_role_comes_from_db_not_from_message(runtime):
    # Username/claims in the update are ignored: only telegram_id + DB matter.
    ev = update(f"/papel {STRANGER} admin", user_id=STRANGER)
    out = runtime.handlers.handle_update(ev)
    assert "restrita" in out[0].text.lower()
    assert runtime.context.users.get_role(STRANGER) is UserRole.PUBLIC

    admin_ev = IncomingUpdate(update_id=2, chat_id=1, user_id=ADMIN,
                              username="not-admin-name", text=f"/papel {STRANGER} admin")
    runtime.handlers.handle_update(admin_ev)
    assert runtime.context.users.get_role(STRANGER) is UserRole.ADMIN


def test_admin_cannot_demote_self(runtime):
    text = say(runtime, f"/papel {ADMIN} public", ADMIN)
    assert "próprio acesso" in text
    assert runtime.context.users.get_role(ADMIN) is UserRole.ADMIN


def test_public_user_cannot_submit(runtime):
    text = say(runtime, f"/novo X | Y | {FUTURE} | https://a.b | d", STRANGER)
    assert "restrita" in text.lower()


def test_editor_submits_event_pending(runtime):
    text = say(runtime, f"/novo Mostra | MAM | {FUTURE} | https://mam.ba/mostra | Texto", EDITOR)
    assert "aguardando revisão" in text
    events = runtime.context.events.list_by_status([EventStatus.PENDING])
    assert len(events) == 1
    assert events[0].authorization_confirmed is False


# --------------------------------------------------------------------------
# approval workflow
# --------------------------------------------------------------------------
def test_approve_requires_organiser_authorization(runtime):
    event = new_event(runtime)
    text = say(runtime, f"/aprovar {event.id}", ADMIN)
    assert "autorização" in text.lower()
    assert runtime.context.events.get(event.id).status is EventStatus.PENDING


def test_authorize_then_approve(runtime):
    event = new_event(runtime)
    say(runtime, f"/autorizar {event.id} e-mail 01/11", ADMIN)
    say(runtime, f"/aprovar {event.id}", ADMIN)
    saved = runtime.context.events.get(event.id)
    assert saved.authorization_confirmed is True
    assert saved.status is EventStatus.APPROVED
    assert "e-mail 01/11" in saved.authorization_reference


def test_reject_requires_reason(runtime):
    event = new_event(runtime)
    assert "motivo" in say(runtime, f"/rejeitar {event.id}", ADMIN).lower()
    say(runtime, f"/rejeitar {event.id} duplicado", ADMIN)
    assert runtime.context.events.get(event.id).status is EventStatus.REJECTED


# --------------------------------------------------------------------------
# publication
# --------------------------------------------------------------------------
def test_dry_run_is_default_and_sends_nothing(runtime):
    event = approved_event(runtime, authorized=True)
    text = say(runtime, f"/publicar {event.id}", ADMIN)
    assert "DRY-RUN" in text
    assert runtime.transport.events == []
    assert runtime.context.events.get(event.id).status is EventStatus.APPROVED


def test_execute_refused_when_deployment_flag_off(runtime):
    event = approved_event(runtime, authorized=True)
    text = say(runtime, f"/publicar {event.id} confirmar {CONFIRMATION_PHRASE}", ADMIN)
    assert "bloqueada" in text.lower()
    assert "desativada" in text.lower()
    assert runtime.transport.events == []


def test_execute_requires_confirmation_phrase(tmp_path):
    rt = build_runtime(make_settings(tmp_path, allow_execute=True), FakeTransport())
    rt.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
    event = approved_event(rt, authorized=True)
    with pytest.raises(PublishBlocked):
        rt.publisher.publish(event.id, ADMIN, dry_run=False, confirmation="sim")
    assert rt.transport.events == []
    assert "exatamente" in say(rt, f"/publicar {event.id} confirmar", ADMIN).lower()


def test_execute_publishes_once_and_is_idempotent(tmp_path):
    rt = build_runtime(make_settings(tmp_path, allow_execute=True), FakeTransport())
    rt.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
    event = approved_event(rt, authorized=True)

    result = rt.publisher.publish(event.id, ADMIN, dry_run=False,
                                  confirmation=CONFIRMATION_PHRASE)
    assert result.outcome == "sent"
    assert rt.transport.events == [(event.id, CHANNEL)]

    saved = rt.context.events.get(event.id)
    assert saved.status is EventStatus.PUBLISHED
    assert saved.published_at and saved.publish_reference == "pub-1"

    again = rt.publisher.publish(event.id, ADMIN, dry_run=False,
                                 confirmation=CONFIRMATION_PHRASE)
    assert again.outcome == "skipped"
    assert len(rt.transport.events) == 1


def test_publish_blocked_without_source_and_description(tmp_path):
    rt = build_runtime(make_settings(tmp_path, allow_execute=True), FakeTransport())
    rt.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
    event = approved_event(rt, authorized=True, source="")
    with pytest.raises(PublishBlocked) as exc:
        rt.publisher.publish(event.id, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE)
    assert any("fonte" in r for r in exc.value.reasons)
    assert rt.transport.events == []


def test_publish_blocked_when_event_in_the_past(tmp_path):
    rt = build_runtime(make_settings(tmp_path, allow_execute=True), FakeTransport())
    rt.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
    event = approved_event(rt, authorized=True)
    rt.context.events.update(Event(**{**rt.context.events.get(event.id).__dict__,
                                      "starts_at": "2020-01-01T10:00"}))
    with pytest.raises(PublishBlocked) as exc:
        rt.publisher.publish(event.id, ADMIN, dry_run=True)
    assert any("ocorreu" in r for r in exc.value.reasons)


def test_publish_requires_admin(runtime):
    event = approved_event(runtime, authorized=True)
    with pytest.raises(PermissionDenied):
        runtime.publisher.publish(event.id, EDITOR, dry_run=True)
    assert runtime.transport.events == []


def test_rate_limit_between_sends(tmp_path):
    rt = build_runtime(make_settings(tmp_path, allow_execute=True), FakeTransport())
    rt.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
    first = approved_event(rt, authorized=True)
    second = approved_event(rt, authorized=True, title="Outro Show")
    rt.publisher.publish(first.id, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE)
    with pytest.raises(PublishBlocked) as exc:
        rt.publisher.publish(second.id, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE)
    assert any("intervalo" in r for r in exc.value.reasons)
    assert len(rt.transport.events) == 1


def test_batch_respects_cap(tmp_path):
    rt = build_runtime(make_settings(tmp_path, allow_execute=True, max_per_run=2), FakeTransport())
    rt.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
    for i in range(4):
        new_event(rt, authorized=True, title=f"Evento {i}")
        rt.context.events.update(Event(**{**rt.context.events.list_by_status(
            [EventStatus.PENDING])[0].__dict__, "status": EventStatus.APPROVED}))
    results = rt.publisher.publish_pending(ADMIN, dry_run=True)
    assert len(results) == 2
    assert all(r.outcome == "dry_run" for r in results)
    assert rt.transport.events == []


def test_transport_failure_is_recorded_not_hidden(tmp_path):
    rt = build_runtime(make_settings(tmp_path, allow_execute=True), FakeTransport(fail=True))
    rt.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
    event = approved_event(rt, authorized=True)
    with pytest.raises(PublishBlocked):
        rt.publisher.publish(event.id, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE)
    assert rt.context.events.get(event.id).status is EventStatus.APPROVED
    attempts = rt.context.__dict__  # not exposed; check audit instead
    actions = [e.action for e in rt.context.audit.recent()]
    assert "publish.execute" in actions


def test_plan_reports_blockers(runtime):
    event = new_event(runtime)
    blockers = runtime.publisher.plan(event.id)
    assert any("aprovado" in b for b in blockers)
    assert any("autorização" in b for b in blockers)


# --------------------------------------------------------------------------
# traceability & hygiene
# --------------------------------------------------------------------------
def test_audit_trail_covers_full_lifecycle(runtime):
    text = say(runtime, f"/novo Mostra X | MAM | {FUTURE} | https://mam.ba/x | Texto", EDITOR)
    event_id = int(re.search(r"#(\d+)", text).group(1))
    say(runtime, f"/autorizar {event_id} e-mail", ADMIN)
    say(runtime, f"/aprovar {event_id}", ADMIN)
    say(runtime, f"/publicar {event_id}", ADMIN)
    entries = runtime.context.audit.for_entity("event", str(event_id))
    actions = [e.action for e in entries]
    for expected in ("event.create", "event.authorize", "event.approve", "publish.dry_run"):
        assert expected in actions
    assert all(e.actor_id is not None for e in entries)


def test_throttle_blocks_flood(runtime):
    runtime.context.throttle = Throttle(max_events=2, window_seconds=60)
    assert "/ajuda" in say(runtime, "/ajuda")
    assert "/ajuda" in say(runtime, "/ajuda")
    assert "Muitas mensagens" in say(runtime, "/ajuda")


def test_default_transport_is_null(tmp_path):
    rt = build_runtime(make_settings(tmp_path))
    assert isinstance(rt.transport, NullTransport)
    assert rt.publisher.status()["allow_execute"] is False


def test_no_event_leaks_into_public_agenda_when_rejected(runtime):
    event = new_event(runtime, authorized=True)
    say(runtime, f"/rejeitar {event.id} informação incompleta", ADMIN)
    assert event.title not in say(runtime, "/agenda")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_init_and_publish_dry_run(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert admin_cli.main(["--db", db, "init", "--admin", str(ADMIN)]) == 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    users = SqliteUserRepository(conn)
    assert users.get_role(ADMIN) is UserRole.ADMIN

    events = SqliteEventRepository(conn)
    ev = events.add(Event(title="Cortejo", venue="Centro", starts_at=FUTURE,
                          description="Cortejo de abertura", source_url="https://x.y/z",
                          submitted_by=EDITOR, status=EventStatus.APPROVED,
                          authorization_confirmed=True,
                          authorization_reference="ofício 12/2026"))
    conn.close()

    assert admin_cli.main(["--db", db, "publish", str(ev.id), "--actor", str(ADMIN)]) == 0
    out = capsys.readouterr().out
    assert "dry_run" in out
