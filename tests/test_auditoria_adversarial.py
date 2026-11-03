"""Testes adversariais: cada caso ataca uma trava em vez de confirmar o caminho feliz.

Cobrem os pontos em que a suíte anterior passava por acidente:

* portão canônico ausente (fail-closed, não fail-open);
* frase de confirmação parcial (`PUBLICAR` sozinha);
* publicação fabricada direto no repositório;
* auditoria que não mostrava rejeição/ingestão em /auditoria;
* identidade forjada (`user_id == 0`) e autor rebaixado;
* token do Telegram em repr/exceção;
* curingas do LIKE em /buscar;
* crash entre envio e mudança de status;
* `TARGET_CHAT_ID=0`, `ADMIN_IDS` inválido, `TELEGRAM_API_BASE` não-http.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from bot.config import Settings
from bot.db import (
    SqliteEventRepository,
    SqlitePublishAttemptRepository,
    SqliteUserRepository,
    connect,
    init_schema,
)
from bot.handlers import BotHandlers, Throttle, sanitize
from bot.models import (
    Event,
    EventStatus,
    PermissionDenied,
    PublishBlocked,
    UserRole,
    ValidationError,
)
from bot.permissions import PermissionService
from bot.publisher import CONFIRMATION_PHRASE, Publisher, PublisherConfig
from bot.runtime import build_runtime
from bot.telegram_api import HttpTransport, IncomingUpdate, OutgoingMessage

ADMIN = 7001
EDITOR = 7002
CHANNEL = -1009876543210
FUTURE = "2027-05-09T19:00"


# ---------------------------------------------------------------------------
# infraestrutura mínima, sem rede
# ---------------------------------------------------------------------------
class FakeTransport:
    def __init__(self):
        self.messages: list = []
        self.events: list[tuple[int, int]] = []

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
        min_interval_seconds=0,
        admin_ids=(ADMIN,),
        api_base="https://api.telegram.org",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def rt(tmp_path):
    runtime = build_runtime(settings_for(tmp_path), FakeTransport())
    runtime.context.users.upsert(EDITOR, "editor", UserRole.EDITOR)
    yield runtime
    runtime.conn.close()


def update(texto: str, user_id: int = ADMIN) -> IncomingUpdate:
    return IncomingUpdate(update_id=1, chat_id=555, user_id=user_id,
                          username="quem", text=texto)


def say(runtime, texto: str, user_id: int = ADMIN) -> str:
    return "\n".join(m.text for m in runtime.handlers.handle_update(update(texto, user_id)))


def submitted(runtime, *, status=EventStatus.PENDING, authorized=True,
              submitted_by=EDITOR, municipio="Salvador") -> Event:
    return runtime.context.events.add(
        Event(
            title="Ensaio aberto da Filarmônica",
            venue="Teatro Castro Alves",
            starts_at=FUTURE,
            description="Ensaio aberto ao público, entrada gratuita.",
            source_url="https://exemplo.org/ensaio",
            submitted_by=submitted_by,
            municipio=municipio,
            status=status,
            authorization_confirmed=authorized,
            authorization_reference="e-mail da produção 01/11/2026" if authorized else "",
        )
    )


# ---------------------------------------------------------------------------
# 1. portão canônico: ausência do hook tem de bloquear, não liberar
# ---------------------------------------------------------------------------
class _RepoSemPortao:
    """Repositório que "esquece" publication_gaps (dependência incompleta)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, nome):
        if nome == "publication_gaps":
            raise AttributeError(nome)
        return getattr(self._real, nome)


def test_repositorio_sem_portao_canonico_nao_publica(rt):
    evento = submitted(rt, status=EventStatus.APPROVED)
    publisher = Publisher(
        _RepoSemPortao(rt.context.events),
        SqlitePublishAttemptRepository(rt.conn),
        rt.context.audit,
        rt.context.permissions,
        rt.transport,
        PublisherConfig(target_chat_id=CHANNEL, allow_execute=True),
    )
    assert "portao_canonico_indisponivel" in publisher.plan(evento.id)
    with pytest.raises(PublishBlocked) as exc:
        publisher.publish(evento.id, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE)
    assert any("portao_canonico_indisponivel" in r for r in exc.value.reasons)
    assert rt.transport.events == []


# ---------------------------------------------------------------------------
# 2. "publicado" não pode ser fabricado pelo repositório
# ---------------------------------------------------------------------------
def test_add_recusa_evento_ja_publicado(rt):
    with pytest.raises(ValidationError) as exc:
        submitted(rt, status=EventStatus.PUBLISHED)
    assert "public" in str(exc.value).lower()
    assert rt.conn.execute(
        "SELECT COUNT(*) FROM tentativas_publicacao WHERE resultado = 'sent'"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 3. frase de confirmação: só a frase completa
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cauda", [
    "PUBLICAR",
    "publicar",
    "confirmar",
    "PUBLICAR confirmar",
    "confirmar PUBLICAR agora",
    "confirmar PUBLICARX",
])
def test_publicar_recusa_frase_parcial(tmp_path, cauda):
    runtime = build_runtime(settings_for(tmp_path, allow_execute=True), FakeTransport())
    try:
        runtime.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
        evento = submitted(runtime, status=EventStatus.APPROVED)
        texto = say(runtime, f"/publicar {evento.id} {cauda}")
        assert "exatamente" in texto.lower(), texto
        assert runtime.transport.events == []
        # Nada foi publicado nem "quase publicado".
        assert runtime.context.events.get(evento.id).status is EventStatus.APPROVED
    finally:
        runtime.conn.close()


def test_publicar_aceita_frase_completa_com_caixa_diferente(tmp_path):
    runtime = build_runtime(settings_for(tmp_path, allow_execute=True), FakeTransport())
    try:
        runtime.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
        evento = submitted(runtime, status=EventStatus.APPROVED)
        texto = say(runtime, f"/publicar {evento.id} Confirmar   publicar")
        assert "publicado" in texto.lower(), texto
        assert runtime.transport.events == [(evento.id, CHANNEL)]
    finally:
        runtime.conn.close()


def test_lote_recusa_confirmacao_parcial(tmp_path):
    runtime = build_runtime(settings_for(tmp_path, allow_execute=True), FakeTransport())
    try:
        runtime.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
        submitted(runtime, status=EventStatus.APPROVED)
        assert "exatamente" in say(runtime, "/lote PUBLICAR").lower()
        assert runtime.transport.events == []
    finally:
        runtime.conn.close()


# ---------------------------------------------------------------------------
# 4. auditoria: /auditoria tem de mostrar rejeição e ingestão
# ---------------------------------------------------------------------------
def test_auditoria_inclui_rejeicao_gravada_pelo_nucleo(rt):
    evento = submitted(rt)
    assert "rejeitado" in say(rt, f"/rejeitar {evento.id} dados incompletos").lower()
    texto = say(rt, f"/auditoria {evento.id}")
    assert "event.reject" in texto
    assert "dados incompletos" in texto


def test_for_entity_le_os_dois_nomes_de_entidade(rt):
    evento = submitted(rt)
    rt.context.audit.log(ADMIN, "event.approve", "event", str(evento.id), "allowed", "bot")
    rt.context.events.reject(evento.id, ADMIN, "motivo")
    acoes = [e.action for e in rt.context.audit.for_entity("evento", str(evento.id))]
    assert "event.approve" in acoes and "event.reject" in acoes


# ---------------------------------------------------------------------------
# 5. identidade: autor zero e autor rebaixado não veem a visão interna
# ---------------------------------------------------------------------------
def test_evento_do_coletor_nao_entrega_visao_interna_para_user_zero(rt):
    evento = submitted(rt, status=EventStatus.APPROVED, submitted_by=0)
    texto = rt.handlers.cmd_evento(555, 0, str(evento.id))[0].text
    assert "— interno —" not in texto
    assert "Autorização" not in texto


def test_autor_rebaixado_perde_visao_interna(rt):
    evento = submitted(rt)
    assert "— interno —" in say(rt, f"/evento {evento.id}", EDITOR)

    rt.context.users.upsert(EDITOR, "", UserRole.PUBLIC)
    texto = say(rt, f"/evento {evento.id}", EDITOR)
    assert "— interno —" not in texto
    assert "Não foi possível" in texto or "não encontrado" in texto


# ---------------------------------------------------------------------------
# 6. sanitização e busca
# ---------------------------------------------------------------------------
def test_sanitize_remove_bidi_e_controles_c1():
    sujo = "\u202eAgenda\x07\x1b\x7f\u200b\u2066 Salvador\ufeff"
    assert sanitize(sujo) == "Agenda Salvador"
    assert sanitize("linha 1\nlinha 2") == "linha 1\nlinha 2"


def test_busca_nao_aceita_curinga_do_like(rt):
    evento = submitted(rt, status=EventStatus.APPROVED)
    assert rt.context.events.search("Ensaio")[0].id == evento.id
    assert rt.context.events.search("%") == []
    assert rt.context.events.search("_") == []
    assert rt.context.events.search("%n_saio%") == []


# ---------------------------------------------------------------------------
# 7. token: nunca em repr nem em exceção
# ---------------------------------------------------------------------------
def test_repr_do_transporte_oculta_token():
    token = "123456789" + ":" + "AAFakeFakeFakeFakeFakeFakeFakeFake1"
    transporte = HttpTransport(token)
    assert token not in repr(transporte)
    assert token not in str(transporte)


def test_falha_de_rede_nao_vaza_token_na_excecao(monkeypatch):
    token = "987654321" + ":" + "BBFakeFakeFakeFakeFakeFakeFakeFake2"

    def estourar(req, timeout=None):
        # urllib coloca a URL completa (com o token) no reason em várias falhas.
        raise urllib.error.URLError(f"<urlopen error timed out: {req.full_url}>")

    monkeypatch.setattr(urllib.request, "urlopen", estourar)
    transporte = HttpTransport(token)
    with pytest.raises(RuntimeError) as exc:
        transporte.send_message(OutgoingMessage(chat_id=1, text="oi"))
    assert token not in str(exc.value)
    assert "<token>" in str(exc.value)


# ---------------------------------------------------------------------------
# 8. crash entre envio e mudança de status
# ---------------------------------------------------------------------------
def test_crash_apos_envio_nao_esconde_publicacao(rt):
    evento = submitted(rt, status=EventStatus.APPROVED)
    real = rt.context.events

    class _UpdateQuebrado:
        def __getattr__(self, nome):
            return getattr(real, nome)

        def update(self, _evento):
            raise RuntimeError("banco travou depois do envio")

    publisher = Publisher(
        _UpdateQuebrado(), SqlitePublishAttemptRepository(rt.conn), rt.context.audit,
        rt.context.permissions, rt.transport,
        PublisherConfig(target_chat_id=CHANNEL, allow_execute=True),
    )
    resultado = publisher.publish(evento.id, ADMIN, dry_run=False,
                                 confirmation=CONFIRMATION_PHRASE)
    assert resultado.outcome == "sent"
    assert rt.transport.events == [(evento.id, CHANNEL)]
    # O fato externo ficou registrado e a leitura deriva "publicado".
    assert rt.conn.execute(
        "SELECT resultado FROM tentativas_publicacao WHERE evento_id=?", (evento.id,)
    ).fetchone()[0] == "sent"
    assert real.get(evento.id).status is EventStatus.PUBLISHED


# ---------------------------------------------------------------------------
# 9. configuração: canal, admins e base da API
# ---------------------------------------------------------------------------
def test_chat_id_zero_nao_e_canal_configurado(tmp_path):
    cfg = Settings.from_env({"TARGET_CHAT_ID": "0", "ADMIN_IDS": "0,-5,123"})
    assert cfg.target_chat_id is None
    assert cfg.admin_ids == (123,)


def test_allow_execute_sem_canal_bloqueia(tmp_path):
    runtime = build_runtime(
        settings_for(tmp_path, allow_execute=True, target_chat_id=None), FakeTransport()
    )
    try:
        runtime.context.users.upsert(EDITOR, "e", UserRole.EDITOR)
        evento = submitted(runtime, status=EventStatus.APPROVED)
        texto = say(runtime, f"/publicar {evento.id} confirmar PUBLICAR")
        assert "canal" in texto.lower()
        assert runtime.transport.events == []
    finally:
        runtime.conn.close()


def test_api_base_invalida_e_recusada():
    with pytest.raises(ValueError):
        Settings.from_env({"TELEGRAM_API_BASE": "file:///etc/passwd"})


# ---------------------------------------------------------------------------
# 10. outros limites: throttle, datas locais, município, /corrigir
# ---------------------------------------------------------------------------
def test_throttle_poda_janelas_vencidas():
    throttle = Throttle(max_events=1, window_seconds=60)
    for uid in range(1, 1100):
        assert throttle.allow(uid, now=0.0)
    assert len(throttle._hits) >= 1024
    assert throttle.allow(5000, now=10_000.0)
    assert len(throttle._hits) == 1


def test_horario_local_nao_desloca_tres_horas(rt):
    evento = submitted(rt, status=EventStatus.APPROVED)
    lido = rt.context.events.get(evento.id)
    assert lido.starts_at == FUTURE
    assert not [b for b in rt.publisher.plan(evento.id) if "data" in b], rt.publisher.plan(evento.id)


def test_municipio_ausente_sem_evidencia_bloqueia(rt):
    evento = submitted(rt, status=EventStatus.APPROVED, municipio="")
    assert any("municipio" in b for b in rt.publisher.plan(evento.id))


def test_corrigir_nao_permite_mexer_em_status(rt):
    evento = submitted(rt)
    texto = say(rt, f"/corrigir {evento.id} status=aprovado")
    assert "não corrigível" in texto
    assert rt.context.events.get(evento.id).status is EventStatus.PENDING


def test_comentario_de_campo_nao_aceita_autorizacao(rt):
    evento = submitted(rt, authorized=False)
    for campo in ("authorization_confirmed", "autorizacao_referencia", "enviado_por"):
        assert "não corrigível" in say(rt, f"/corrigir {evento.id} {campo}=1")
    atual = rt.context.events.get(evento.id)
    assert atual.authorization_confirmed is False
    assert atual.submitted_by == EDITOR


def test_publico_nao_le_evento_pendente(rt):
    evento = submitted(rt)
    assert "não encontrado" in say(rt, f"/evento {evento.id}", 999_999).lower()


def test_sem_admin_nao_ha_como_promover(rt):
    with pytest.raises(PermissionDenied):
        rt.handlers.cmd_papel(555, EDITOR, f"{EDITOR} admin")
    assert SqliteUserRepository(rt.conn).get_role(EDITOR) is UserRole.EDITOR


def test_repositorio_de_eventos_e_o_canonico(tmp_path):
    conn = connect(tmp_path / "canonico.db")
    init_schema(conn)
    try:
        assert hasattr(SqliteEventRepository(conn), "publication_gaps")
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
    finally:
        conn.close()
