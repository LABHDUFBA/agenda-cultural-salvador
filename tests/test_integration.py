"""Testes de integração reais: coleta -> núcleo canônico -> bot/moderação.

Nada aqui toca a rede: o fetcher é um dublê que devolve a fixture HTML real do
calendário do TCA (``tests/fixtures/tca_calendario.html``), então o caminho
exercitado é o mesmo da produção até a porta do HTTP.

O que estes testes provam:

* existe **um** banco e **um** schema (``eventos``), não dois;
* coleta grava proveniência e nasce ``pendente`` — nunca aprovada;
* duplicata exata não cria segunda linha;
* fonte bloqueada não é ingerida;
* evento fora de Salvador é rejeitado e fica auditado;
* o bot consulta a mesma base e só mostra o que foi aprovado;
* publicação exige evidências canônicas (Salvador, data local, local, fonte)
  e continua dry-run + frase + flag, sem enviar nada por padrão;
* "publicado" é derivado de ``tentativas_publicacao``, não de um segundo
  status;
* migração do banco antigo do bot preserva as linhas existentes.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.config import Settings
from bot.db import SqliteEventRepository
from bot.models import Event, EventStatus, UserRole
from bot.publisher import CONFIRMATION_PHRASE, PublisherConfig
from bot.runtime import build_runtime
from collectors.bagov import ColetorBaGovCalendario
from collectors.fetch import PaginaBaixada
from collectors.ingest import FonteNaoColetavel, fila_de_revisao, ingerir_coletados
from collectors.models import EventoColetado
from database import abrir_banco, inicializar, listar_pendentes, obter_evento
from database.connection import COLUNAS_ADICIONADAS, SCHEMA_VERSION
from sources.catalog import por_id

RAIZ = Path(__file__).resolve().parents[1]
FIXTURE = RAIZ / "tests" / "fixtures" / "tca_calendario.html"
ADMIN = 7001
EDITOR = 7002
CHANNEL = -1009876543210
# Coordenadas reais do Teatro Castro Alves (Campo Grande, Salvador-BA). Usadas
# como a evidência factual que a curadoria registra ao preencher o local.
COORDENADAS_TCA = (-12.9816, -38.5186)
FUTURO = (datetime.now(timezone.utc) + timedelta(days=45)).astimezone(
    timezone(timedelta(hours=-3))
).replace(second=0, microsecond=0)


class StubFetcher:
    """Devolve a fixture e nunca abre socket."""

    def __init__(self, html: str):
        self.html = html

    def baixar(self, url: str, *, fonte_id: str = "") -> PaginaBaixada:
        return PaginaBaixada(
            url_pedida=url,
            url_final=url,
            status=200,
            content_type="text/html; charset=utf-8",
            texto=self.html,
            tamanho=len(self.html),
            buscado_em=datetime.now(timezone.utc),
            duracao_s=0.0,
        )

    def sondar(self, url: str, *, fonte_id: str = ""):
        return {"acessivel": True, "fonte_id": fonte_id}


class FakeTransport:
    def __init__(self):
        self.events: list[tuple[int, int]] = []
        self.messages: list = []

    def send_message(self, message):
        self.messages.append(message)
        return f"msg-{len(self.messages)}"

    def send_event(self, event, chat_id):
        self.events.append((event.id, chat_id))
        return f"pub-{len(self.events)}"

    def get_updates(self, offset, timeout=25):
        return []


@pytest.fixture()
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture()
def banco(tmp_path: Path):
    conn = abrir_banco(tmp_path / "agenda.db")
    yield conn
    conn.close()


def coletar_tca(html: str, limite: int | None = None) -> list[EventoColetado]:
    coletor = ColetorBaGovCalendario(por_id("tca"), StubFetcher(html))
    return coletor.coletar(limite=limite)


def fonte_texto(**overrides) -> EventoColetado:
    base = dict(
        fonte_id="teste",
        titulo="Mostra de Arte Baiana",
        inicio_em=FUTURO,
        url_original="https://exemplo.org/mostra",
        metodo_extracao="cartao_html",
        confianca="alta",
        trecho="Mostra de Arte Baiana — 45 dias — 19h — Pelourinho",
        local_nome="Pelourinho",
        url_ingresso="https://exemplo.org/ingressos",
        municipio="Salvador",
        uf="BA",
    )
    base.update(overrides)
    return EventoColetado(**base)


def make_settings(db_path: Path, **overrides) -> Settings:
    base = dict(
        db_path=str(db_path),
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


def runtime_for(db_path: Path, **overrides):
    rt = build_runtime(make_settings(db_path, **overrides), FakeTransport())
    rt.context.users.upsert(EDITOR, "editor", UserRole.EDITOR)
    return rt


def say(rt, text: str, user_id: int = ADMIN) -> str:
    from bot.telegram_api import IncomingUpdate

    out = rt.handlers.handle_update(
        IncomingUpdate(update_id=1, chat_id=555, user_id=user_id, username="x", text=text)
    )
    return "\n".join(m.text for m in out)


# ---------------------------------------------------------------------------
# 1. um único esquema
# ---------------------------------------------------------------------------
def test_um_unico_banco_e_um_unico_esquema(banco):
    tabelas = {
        r["name"] for r in banco.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "eventos" in tabelas
    assert "events" not in tabelas, "o bot não pode manter um segundo núcleo de eventos"
    assert {"usuarios", "tentativas_publicacao"} <= tabelas
    assert "proveniencia" in tabelas and "auditoria" in tabelas


def test_runtime_do_bot_usa_o_repositorio_canonico(tmp_path):
    rt = runtime_for(tmp_path / "x.db")
    assert hasattr(rt.context.events, "publication_gaps")
    tabelas = {
        r["name"]
        for r in rt.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "events" not in tabelas


# ---------------------------------------------------------------------------
# 2. coleta -> persistência
# ---------------------------------------------------------------------------
def test_coleta_real_persiste_pendente_com_proveniencia(banco, html):
    eventos = coletar_tca(html)
    assert len(eventos) == 3

    resultado = ingerir_coletados(banco, por_id("tca"), eventos)
    assert (resultado.coletados, resultado.aceitos, resultado.rejeitados) == (3, 3, 0)

    linhas = listar_pendentes(banco)
    assert len(linhas) == 3
    assert all(linha["status"] == "pendente" for linha in linhas)
    assert all(linha["municipio"] == "Salvador" and linha["uf"] == "BA" for linha in linhas)

    fonte = banco.execute(
        "SELECT nome, url_base, confiabilidade FROM fontes WHERE id = ?",
        (resultado.fonte_id,),
    ).fetchone()
    assert fonte["confiabilidade"] == "alta"
    assert fonte["url_base"].startswith("https://www.ba.gov.br/tca")

    provs = [
        p
        for linha in linhas
        for p in obter_evento(banco, linha["id"])["proveniencia"]
    ]
    assert len(provs) == 3
    assert all(p["url_original"].startswith("https://") for p in provs)
    assert all(len(p["hash_conteudo"]) == 64 for p in provs)
    assert all(p["sessao_id"] == resultado.sessao_id for p in provs)

    sessao = banco.execute(
        "SELECT status, finalizada_em FROM sessoes WHERE id = ?", (resultado.sessao_id,)
    ).fetchone()
    assert sessao["status"] == "concluida" and sessao["finalizada_em"]


def test_duplicata_exata_nao_cria_segunda_linha(banco, html):
    eventos = coletar_tca(html)
    primeiro = ingerir_coletados(banco, por_id("tca"), eventos)
    segundo = ingerir_coletados(banco, por_id("tca"), eventos)

    assert primeiro.aceitos == 3
    assert segundo.duplicados == 3 and segundo.aceitos == 0
    total = banco.execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
    assert total == 3, "duplicata exata não pode duplicar o evento"
    acoes = [r["acao"] for r in banco.execute("SELECT acao FROM auditoria")]
    assert acoes.count("duplicado") == 3


def test_fonte_bloqueada_nao_e_ingerida(banco, html):
    with pytest.raises(FonteNaoColetavel):
        ingerir_coletados(banco, por_id("muncab"), coletar_tca(html))
    assert banco.execute("SELECT COUNT(*) FROM eventos").fetchone()[0] == 0
    assert banco.execute("SELECT COUNT(*) FROM sessoes").fetchone()[0] == 0


def test_evento_fora_de_salvador_e_rejeitado_e_auditado(banco):
    # `municipio` diferente de Salvador é rejeitado pelo núcleo, mesmo com
    # confiança alta do coletor.
    evento = fonte_texto(municipio="Recife", uf="PE")
    resultado = ingerir_coletados(banco, por_id("tca"), [evento])
    assert resultado.rejeitados == 1 and resultado.aceitos == 0

    linha = banco.execute("SELECT status, motivo FROM eventos").fetchone()
    assert linha["status"] == "rejeitado"
    assert "fora_de_salvador" in linha["motivo"]
    assert listar_pendentes(banco) == []

    auditoria = banco.execute(
        "SELECT detalhe FROM auditoria WHERE acao = 'ingestao_rejeitada'"
    ).fetchone()
    assert "fora_de_salvador" in auditoria["detalhe"].lower()


def test_municipio_declarado_sem_evidencia_nao_publica(banco):
    """"Salvador" no campo é declaração; publicar exige prova independente."""
    from database.repository import requisitos_publicacao_evento
    from database.validation import evidencia_de_municipio

    evento = fonte_texto(trecho="Mostra de Arte Baiana — Pelourinho, 19h")
    resultado = ingerir_coletados(banco, por_id("tca"), [evento])
    alvo = resultado.resultados[0].evento_id
    with banco:
        banco.execute(
            "UPDATE eventos SET status='aprovado', local_nome='Pelourinho', "
            "autorizacao_confirmada=1, autorizacao_referencia='e-mail 01/11' "
            "WHERE id=?", (alvo,),
        )
    linha = banco.execute("SELECT * FROM eventos WHERE id=?", (alvo,)).fetchone()
    provs = [dict(p) for p in banco.execute(
        "SELECT trecho FROM proveniencia WHERE evento_id=?", (alvo,))]
    assert evidencia_de_municipio(dict(linha), provs) == ""
    faltas = requisitos_publicacao_evento(banco, alvo)
    assert "municipio_salvador_sem_evidencia" in faltas, faltas


def test_municipio_comprovado_pelo_texto_da_fonte(banco):
    from database.repository import requisitos_publicacao_evento

    evento = fonte_texto(trecho="Mostra de Arte Baiana — Pelourinho, Salvador-BA, 19h")
    resultado = ingerir_coletados(banco, por_id("tca"), [evento])
    alvo = resultado.resultados[0].evento_id
    with banco:
        banco.execute(
            "UPDATE eventos SET status='aprovado', local_nome='Pelourinho', "
            "autorizacao_confirmada=1, autorizacao_referencia='e-mail 01/11' "
            "WHERE id=?", (alvo,),
        )
    faltas = requisitos_publicacao_evento(banco, alvo)
    assert not [f for f in faltas if f.startswith("municipio_")], faltas


def test_coordenadas_fora_da_cidade_contradizem_municipio(banco):
    """Coordenada de Recife + município Salvador: o portão não escolhe lado."""
    from database.repository import requisitos_publicacao_evento

    evento = fonte_texto(trecho="Mostra de Arte Baiana — Pelourinho, 19h")
    resultado = ingerir_coletados(banco, por_id("tca"), [evento])
    alvo = resultado.resultados[0].evento_id
    with banco:
        banco.execute(
            "UPDATE eventos SET status='aprovado', local_nome='Pelourinho', "
            "latitude=-8.05, longitude=-34.88, autorizacao_confirmada=1, "
            "autorizacao_referencia='e-mail 01/11' WHERE id=?", (alvo,),
        )
    faltas = requisitos_publicacao_evento(banco, alvo)
    assert "municipio_salvador_contradito_por_coordenadas" in faltas, faltas


def test_confianca_baixa_e_inferencias_viram_motivo_de_revisao(banco):
    evento = fonte_texto(
        confianca="baixa",
        campos_inferidos=("hora", "ano"),
        metodo_extracao="texto_agenda",
        local_nome="",
    )
    resultado = ingerir_coletados(banco, por_id("tca"), [evento])
    assert resultado.aceitos == 1

    linha = banco.execute("SELECT status, motivo FROM eventos").fetchone()
    assert linha["status"] == "pendente"
    assert "revisao_obrigatoria:confianca_baixa" in linha["motivo"]
    assert "campos_inferidos:ano,hora" in linha["motivo"]
    assert "local_ausente_na_fonte" in linha["motivo"]


# ---------------------------------------------------------------------------
# 3. consulta e moderação no mesmo banco
# ---------------------------------------------------------------------------
def test_bot_le_a_mesma_base_e_esconde_pendentes(banco, html, tmp_path):
    db = tmp_path / "agenda.db"
    ingerir_coletados(banco, por_id("tca"), coletar_tca(html))
    banco.close()

    rt = runtime_for(db)
    try:
        agenda = say(rt, "/agenda", user_id=EDITOR)
        assert "Nenhum evento aprovado" in agenda

        fila = say(rt, "/pendentes", user_id=ADMIN)
        assert "OSBA em Série Jorge Amado" in fila
        assert "SEM autorização" in fila
    finally:
        rt.conn.close()


def test_aprovacao_humana_leva_o_evento_coletado_ao_publico(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    resultado = ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    primeiro = conn.execute("SELECT id FROM eventos ORDER BY id LIMIT 1").fetchone()["id"]
    # Curadoria humana preenche o local que o calendário do TCA não publica.
    with conn:
        conn.execute("UPDATE eventos SET local_nome = ? WHERE id = ?",
                     ("Teatro Castro Alves", primeiro))
    conn.close()

    rt = runtime_for(db)
    try:
        assert "autorização" in say(rt, f"/aprovar {primeiro}", ADMIN).lower()
        say(rt, f"/autorizar {primeiro} e-mail da produção 01/11", ADMIN)
        say(rt, f"/aprovar {primeiro}", ADMIN)

        agenda = say(rt, "/agenda")
        assert "OSBA em Série Jorge Amado" in agenda
        # os demais continuam fora do público
        assert "João Bosco 80 Anos" not in agenda
        assert resultado.aceitos == 3
    finally:
        rt.conn.close()


def test_rejeitar_mantem_registro_e_proveniencia(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    alvo = conn.execute("SELECT id FROM eventos ORDER BY id LIMIT 1").fetchone()["id"]
    conn.close()

    rt = runtime_for(db)
    try:
        say(rt, f"/rejeitar {alvo} local não publicado pela fonte", ADMIN)
        evento = rt.context.events.get(alvo)
        assert evento.status is EventStatus.REJECTED
        # nada foi apagado
        assert rt.conn.execute(
            "SELECT COUNT(*) FROM proveniencia WHERE evento_id = ?", (alvo,)
        ).fetchone()[0] == 1
        assert "local não publicado" in rt.conn.execute(
            "SELECT motivo FROM eventos WHERE id = ?", (alvo,)
        ).fetchone()["motivo"]
        # O motivo da ingestão (por que o evento estava pendente) não se perde:
        # fica na auditoria append-only, junto do motivo humano e do ator.
        auditoria = rt.conn.execute(
            "SELECT ator, estado_anterior, estado_novo, detalhe FROM auditoria "
            "WHERE acao = 'event.reject' AND entidade_id = ? ORDER BY id DESC LIMIT 1",
            (alvo,),
        ).fetchone()
        assert auditoria is not None, "rejeição precisa de registro de auditoria"
        assert auditoria["estado_anterior"] == "pendente"
        assert auditoria["estado_novo"] == "rejeitado"
        assert auditoria["ator"] == str(ADMIN)
        assert "local não publicado" in auditoria["detalhe"]
        assert "local_ausente_na_fonte" in auditoria["detalhe"]
        # Nada foi apagado do evento nem da proveniência.
        assert rt.conn.execute(
            "SELECT COUNT(*) FROM eventos WHERE id = ?", (alvo,)
        ).fetchone()[0] == 1
    finally:
        rt.conn.close()


# ---------------------------------------------------------------------------
# 4. portão de publicação
# ---------------------------------------------------------------------------
def test_portao_bloqueia_evento_coletado_sem_local(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    resultado = ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    alvo = conn.execute("SELECT id FROM eventos ORDER BY id LIMIT 1").fetchone()["id"]
    # Aprovação editorial sem curadoria do local: o portão ainda barra.
    with conn:
        conn.execute("UPDATE eventos SET status='aprovado', autorizacao_confirmada=1, "
                     "autorizacao_referencia='e-mail 01/11' WHERE id=?", (alvo,))
    assert resultado.aceitos == 3
    conn.close()

    rt = runtime_for(db)
    try:
        blockers = rt.publisher.plan(alvo)
        assert any("local_nao_comprovado" in b for b in blockers), blockers
        say(rt, f"/publicar {alvo}", ADMIN)  # dry-run: devolve bloqueio, não envia

        # Com o local preenchido e a proveniência íntegra, o portão abre.
        with rt.conn:
            rt.conn.execute("UPDATE eventos SET local_nome='Teatro Castro Alves' "
                            "WHERE id=?", (alvo,))
        # ... menos o município: o calendário do TCA não diz "Salvador" e não
        # publica coordenadas, então "Salvador" no campo é só o padrão do
        # coletor. Sem evidência independente, publicar continua bloqueado.
        assert any("municipio_salvador_sem_evidencia" in b
                   for b in rt.publisher.plan(alvo)), rt.publisher.plan(alvo)

        # A curadoria registra a coordenada do teatro (evidência factual).
        with rt.conn:
            rt.conn.execute("UPDATE eventos SET latitude=?, longitude=? WHERE id=?",
                            (*COORDENADAS_TCA, alvo))
        assert rt.publisher.plan(alvo) == []

        # Sem fonte (nem url_fonte nem proveniência) o núcleo barra.
        with rt.conn:
            rt.conn.execute("UPDATE eventos SET url_fonte='' WHERE id=?", (alvo,))
            rt.conn.execute("DELETE FROM proveniencia WHERE evento_id=?", (alvo,))
        gaps = rt.context.events.publication_gaps(alvo)
        assert "fonte_nao_verificavel" in gaps, gaps

        with rt.conn:
            rt.conn.execute("UPDATE eventos SET municipio='Recife' WHERE id=?", (alvo,))
        assert any("municipio_salvador_nao_comprovado" in b
                   for b in rt.publisher.plan(alvo))

        with rt.conn:
            rt.conn.execute("UPDATE eventos SET municipio='Salvador', data_ref='1999-01-01' "
                            "WHERE id=?", (alvo,))
        assert any("data_ref_incoerente_com_inicio" in b
                   for b in rt.publisher.plan(alvo))
    finally:
        rt.conn.close()


def test_rejeitar_evento_publicado_e_recusado(tmp_path, html):
    """Rejeitar algo que já saiu no canal reescreveria um fato externo."""
    from bot.models import ValidationError

    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    alvo = conn.execute("SELECT id FROM eventos ORDER BY id LIMIT 1").fetchone()["id"]
    with conn:
        conn.execute("UPDATE eventos SET local_nome='Teatro Castro Alves', "
                     "latitude=?, longitude=?, status='aprovado', "
                     "autorizacao_confirmada=1, autorizacao_referencia='e-mail 01/11' "
                     "WHERE id=?", (*COORDENADAS_TCA, alvo))
    conn.close()

    rt = runtime_for(db, allow_execute=True)
    try:
        assert rt.publisher.publish(
            alvo, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE
        ).outcome == "sent"

        with pytest.raises(ValidationError):
            rt.context.events.reject(alvo, ADMIN, "informação errada")
        # O fato publicado continua registrado e o status editorial não mudou.
        assert rt.context.events.get(alvo).status is EventStatus.PUBLISHED
        assert rt.conn.execute(
            "SELECT status FROM eventos WHERE id=?", (alvo,)
        ).fetchone()["status"] == "aprovado"
        assert rt.conn.execute(
            "SELECT COUNT(*) FROM tentativas_publicacao "
            "WHERE evento_id=? AND resultado='sent'", (alvo,)
        ).fetchone()[0] == 1
    finally:
        rt.conn.close()


def test_rejeitar_sem_motivo_nao_grava_nada(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    alvo = conn.execute("SELECT id FROM eventos ORDER BY id LIMIT 1").fetchone()["id"]
    conn.close()

    rt = runtime_for(db)
    try:
        assert "motivo" in say(rt, f"/rejeitar {alvo}", ADMIN).lower()
        assert rt.context.events.get(alvo).status is EventStatus.PENDING
        assert rt.conn.execute(
            "SELECT COUNT(*) FROM auditoria WHERE acao='event.reject'"
        ).fetchone()[0] == 0
    finally:
        rt.conn.close()


def test_rejeitar_exige_admin(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    alvo = conn.execute("SELECT id FROM eventos ORDER BY id LIMIT 1").fetchone()["id"]
    conn.close()

    rt = runtime_for(db)
    try:
        say(rt, f"/rejeitar {alvo} motivo qualquer", EDITOR)
        assert rt.context.events.get(alvo).status is EventStatus.PENDING
    finally:
        rt.conn.close()


def test_dry_run_e_padrao_e_nao_grava_publicacao(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    alvo = conn.execute("SELECT id FROM eventos ORDER BY id LIMIT 1").fetchone()["id"]
    with conn:
        conn.execute("UPDATE eventos SET local_nome='Teatro Castro Alves', "
                     "latitude=?, longitude=?, "
                     "status='aprovado', autorizacao_confirmada=1, "
                     "autorizacao_referencia='e-mail 01/11' WHERE id=?",
                     (*COORDENADAS_TCA, alvo))
    conn.close()

    rt = runtime_for(db)
    try:
        texto = say(rt, f"/publicar {alvo}", ADMIN)
        assert "DRY-RUN" in texto
        assert rt.transport.events == [], "dry-run não pode chamar o transporte"

        tentativas = rt.conn.execute(
            "SELECT modo, resultado FROM tentativas_publicacao WHERE evento_id=?", (alvo,)
        ).fetchall()
        assert [(t["modo"], t["resultado"]) for t in tentativas] == [("dry_run", "dry_run")]
        assert rt.context.events.get(alvo).status is EventStatus.APPROVED

        # Envio real desligado no ambiente: bloqueia mesmo com frase correta.
        say(rt, f"/publicar {alvo} confirmar {CONFIRMATION_PHRASE}", ADMIN)
        assert rt.transport.events == []
        assert rt.context.events.get(alvo).status is EventStatus.APPROVED
    finally:
        rt.conn.close()


def test_publicado_deriva_de_tentativas_publicacao(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    alvo = conn.execute("SELECT id FROM eventos ORDER BY id LIMIT 1").fetchone()["id"]
    with conn:
        conn.execute("UPDATE eventos SET local_nome='Teatro Castro Alves', "
                     "latitude=?, longitude=?, "
                     "status='aprovado', autorizacao_confirmada=1, "
                     "autorizacao_referencia='e-mail 01/11' WHERE id=?",
                     (*COORDENADAS_TCA, alvo))
    conn.close()

    rt = runtime_for(db, allow_execute=True)
    try:
        resultado = rt.publisher.publish(
            alvo, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE
        )
        assert resultado.outcome == "sent"
        assert rt.transport.events == [(alvo, CHANNEL)]

        evento = rt.context.events.get(alvo)
        assert evento.status is EventStatus.PUBLISHED
        assert evento.publish_reference == "pub-1"

        linha = rt.conn.execute("SELECT status FROM eventos WHERE id=?", (alvo,)).fetchone()
        assert linha["status"] == "aprovado", "publicar não reescreve o status editorial"

        tentativa = rt.conn.execute(
            "SELECT modo, resultado, referencia FROM tentativas_publicacao "
            "WHERE evento_id=? AND resultado='sent'", (alvo,)
        ).fetchone()
        assert (tentativa["modo"], tentativa["referencia"]) == ("execute", "pub-1")

        outra = rt.publisher.publish(
            alvo, ADMIN, dry_run=False, confirmation=CONFIRMATION_PHRASE
        )
        assert outra.outcome == "skipped"
        assert len(rt.transport.events) == 1, "publicação é idempotente"
    finally:
        rt.conn.close()


def test_evento_submetido_no_bot_passa_pelo_mesmo_portao(tmp_path):
    db = tmp_path / "agenda.db"
    rt = runtime_for(db)
    try:
        texto = say(
            rt,
            f"/novo Mostra X | Pelourinho | {FUTURO.strftime('%Y-%m-%dT%H:%M')} | "
            "https://exemplo.org/mostra | Texto de apoio",
            EDITOR,
        )
        event_id = int(texto.split("#")[1].split(".")[0])
        # Ainda pendente: nem dry-run é permitido.
        assert any("evento_sem_aprovacao_editorial" in b
                   for b in rt.publisher.plan(event_id))
        say(rt, f"/autorizar {event_id} e-mail da produção", ADMIN)
        say(rt, f"/aprovar {event_id}", ADMIN)
        assert rt.publisher.plan(event_id) == []
        assert "DRY-RUN" in say(rt, f"/publicar {event_id}", ADMIN)
        assert rt.transport.events == []
    finally:
        rt.conn.close()


# ---------------------------------------------------------------------------
# 5. preservação de dados / migração
# ---------------------------------------------------------------------------
def test_migracao_do_banco_antigo_do_bot_preserva_linhas(tmp_path):
    path = tmp_path / "legado.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT, venue TEXT,"
        " starts_at TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO events (title, venue, starts_at, status) VALUES (?,?,?,?)",
        ("Evento legado", "Pelourinho", "2026-12-01T19:00", "approved"),
    )
    conn.commit()

    inicializar(conn)

    legado = conn.execute("SELECT * FROM events").fetchall()
    assert len(legado) == 1 and legado[0]["title"] == "Evento legado"

    colunas = {r["name"] for r in conn.execute("PRAGMA table_info(eventos)")}
    assert set(COLUNAS_ADICIONADAS) <= colunas
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM eventos").fetchone()[0] == 0
    conn.close()


def test_reabrir_banco_nao_apaga_nada(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    conn.close()

    conn = abrir_banco(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM eventos").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM proveniencia").fetchone()[0] == 3
        assert inicializar(conn) == SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. CLI de consulta/moderação (sem rede)
# ---------------------------------------------------------------------------
def test_cli_pendentes_json_mostra_pendencias(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    conn.close()

    proc = subprocess.run(
        [sys.executable, "-m", "collectors.cli", "pendentes", "--db", str(db), "--json"],
        cwd=str(RAIZ), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    fila = json.loads(proc.stdout)
    assert len(fila) == 3
    assert all(item["status"] == "pendente" for item in fila)
    assert all("local_nao_comprovado" in item["pendencias_publicacao"] for item in fila)
    assert all(item["proveniencia"] for item in fila)


def test_cli_collect_gravar_recusa_fonte_bloqueada_sem_escrever(tmp_path):
    db = tmp_path / "agenda.db"
    proc = subprocess.run(
        [sys.executable, "-m", "collectors.cli", "collect", "--fonte", "muncab",
         "--gravar", "--db", str(db)],
        cwd=str(RAIZ), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "FonteBloqueada" in proc.stderr
    # Nenhum banco/linha criada pela tentativa recusada.
    if Path(db).exists():
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM eventos").fetchone()[0] == 0
        finally:
            conn.close()


def test_fila_de_revisao_e_somente_leitura(tmp_path, html):
    db = tmp_path / "agenda.db"
    conn = abrir_banco(db)
    ingerir_coletados(conn, por_id("tca"), coletar_tca(html))
    antes = conn.execute("SELECT COUNT(*) FROM auditoria").fetchone()[0]
    fila = fila_de_revisao(conn)
    assert len(fila) == 3
    assert conn.execute("SELECT COUNT(*) FROM auditoria").fetchone()[0] == antes
    conn.close()
