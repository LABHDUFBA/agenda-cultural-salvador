"""Testes das feiras — offline, sem rede.

O teste que importa é ``test_evidencia_confere_com_snapshot``: cada trecho
citado no catálogo tem de existir, byte a byte, no recorte preservado da
fonte. É o que impede "dado plausível" de entrar no banco.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from bot.runtime import build_runtime
from bot.telegram_api import IncomingUpdate
from database import abrir_banco
from feiras import catalogo, repository

RAIZ = Path(__file__).resolve().parents[1]
SNAPSHOTS = RAIZ / "sources" / "feiras"


@pytest.fixture
def conn():
    c = abrir_banco(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. catálogo
# ---------------------------------------------------------------------------
def test_catalogo_tem_feiras_unicas_e_tipos_validos():
    slugs = [f.slug for f in catalogo.FEIRAS]
    assert len(slugs) == len(set(slugs)) == 25
    assert all(f.tipo in ("livre", "cultural", "artesanato", "misto") for f in catalogo.FEIRAS)
    assert all(f.nome.strip() for f in catalogo.FEIRAS)


def test_23_feiras_municipais_tem_dia_publicado():
    com_dia = [f for f in catalogo.FEIRAS if f.dias]
    assert len(com_dia) == 23
    assert all(f.dias == catalogo.TERCA_A_DOMINGO for f in com_dia)


def test_feira_sem_dia_nao_finge_dia():
    porto = catalogo.por_slug("porto-das-sardinhas")
    assert porto.dias == () and porto.status == "parcial"
    se = catalogo.por_slug("feira-artesanato-praca-da-se")
    assert se.dias == () and se.status == "sem_dia_fixo" and se.regular is False


def test_bairro_nao_informado_fica_vazio():
    for feira in catalogo.FEIRAS:
        if feira.bairro_base == "nao_informado":
            assert feira.bairro == "", f"{feira.slug} declara bairro sem base na fonte"
        else:
            assert feira.bairro.strip(), f"{feira.slug} tem base de bairro mas nenhum bairro"


# ---------------------------------------------------------------------------
# 2. evidência x snapshot (a prova)
# ---------------------------------------------------------------------------
def test_toda_fonte_tem_snapshot_preservado():
    for chave, fonte in catalogo.FONTES.items():
        caminho = SNAPSHOTS / fonte.snapshot
        assert caminho.is_file(), f"snapshot ausente para {chave}: {caminho}"
        assert caminho.stat().st_size > 200, f"snapshot suspeito de vazio: {chave}"


def test_evidencia_confere_com_snapshot():
    falhas = []
    for feira in catalogo.FEIRAS:
        for ev in feira.evidencias:
            fonte = catalogo.FONTES[ev.fonte]
            texto = (SNAPSHOTS / fonte.snapshot).read_text(encoding="utf-8")
            if ev.trecho not in texto:
                falhas.append(f"{feira.slug}/{ev.campo}: {ev.trecho!r}")
    assert not falhas, "trechos não encontrados na fonte:\n" + "\n".join(falhas)


def test_feira_verificada_tem_evidencia_de_dia_e_endereco():
    for feira in catalogo.FEIRAS:
        campos = {ev.campo for ev in feira.evidencias}
        if feira.status == "verificado":
            assert {"dias", "endereco"} <= campos, feira.slug
        else:
            assert campos, feira.slug


def test_manipulacao_de_evidencia_e_bloqueada_pelo_schema(conn):
    res = repository.carregar(conn)
    assert res.criadas == 25 and res.evidencias_novas == 55
    ev_id = conn.execute("SELECT id FROM feiras_evidencias LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE feiras_evidencias SET trecho='inventado' WHERE id=?", (ev_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM feiras_evidencias WHERE id=?", (ev_id,))


# ---------------------------------------------------------------------------
# 3. carga idempotente e não destrutiva
# ---------------------------------------------------------------------------
def test_carregar_e_idempotente(conn):
    primeiro = repository.carregar(conn)
    segundo = repository.carregar(conn)
    assert (primeiro.criadas, primeiro.evidencias_novas) == (25, 55)
    assert (segundo.criadas, segundo.atualizadas, segundo.evidencias_novas) == (0, 0, 0)
    assert repository.resumo(conn)["feiras"] == 25


def test_recarregar_preserva_linha_extra(conn):
    repository.carregar(conn)
    agora = "2026-11-03T12:00:00+00:00"
    conn.execute(
        "INSERT INTO feiras (slug, nome, tipo, bairro, bairro_base, endereco, dias,"
        " horario, feirantes, regular, status_verificacao, observacao, criado_em, atualizado_em)"
        " VALUES ('feira-manual','Feira Manual','cultural','','nao_informado','',"
        " 'sabado','',NULL,1,'parcial','inserida à mão',?,?)",
        (agora, agora),
    )
    conn.commit()
    repository.carregar(conn)
    assert conn.execute("SELECT count(*) FROM feiras").fetchone()[0] == 26
    assert repository.obter(conn, "feira-manual") is not None


def test_hash_do_snapshot_fica_no_banco(conn):
    repository.carregar(conn)
    linha = conn.execute(
        "SELECT snapshot_path, hash_snapshot FROM feiras_fontes WHERE chave='semop'"
    ).fetchone()
    assert Path(linha["snapshot_path"]).is_file()
    assert hashlib.sha256(Path(linha["snapshot_path"]).read_bytes()).hexdigest() == linha["hash_snapshot"]


# ---------------------------------------------------------------------------
# 4. consulta por dia / bairro
# ---------------------------------------------------------------------------
def test_listar_por_dia_nao_inventa(conn):
    repository.carregar(conn)
    assert len(repository.listar(conn, dia="sabado")) == 23
    assert repository.listar(conn, dia="segunda") == []
    nomes = {f.slug for f in repository.listar(conn, dia="domingo")}
    assert "porto-das-sardinhas" not in nomes  # dia não publicado


def test_busca_por_bairro_e_termo(conn):
    repository.carregar(conn)
    liberdade = repository.listar(conn, bairro="liberdade")
    assert [f.slug for f in liberdade] == ["japao-liberdade"]
    achadas = repository.buscar(conn, "são joaquim")
    assert [f.slug for f in achadas] == ["sao-joaquim"]


def test_evidencias_de_sao_joaquim_citam_semop(conn):
    repository.carregar(conn)
    feira = repository.obter(conn, "sao-joaquim")
    provas = repository.evidencias(conn, feira.id)
    assert {p.campo for p in provas} >= {"endereco", "dias", "bairro", "programacao_cultural"}
    assert any("7.500" in p.trecho for p in provas)
    assert all(p.fonte_url.startswith("http") for p in provas)


# ---------------------------------------------------------------------------
# 5. bot
# ---------------------------------------------------------------------------
def _rt(tmp_path):
    from bot.config import Settings

    settings = Settings(
        db_path=str(tmp_path / "agenda.db"),
        bot_token=None,
        target_chat_id=-1001234567890,
        allow_execute=False,
        max_per_run=3,
        min_interval_seconds=300,
        admin_ids=(9001,),
        api_base="https://api.telegram.org",
    )
    rt = build_runtime(settings)
    repository.carregar(rt.conn)
    return rt


def _say(rt, texto: str) -> str:
    out = rt.handlers.handle_update(
        IncomingUpdate(update_id=1, chat_id=555, user_id=424242, username="", text=texto)
    )
    return "\n".join(m.text for m in out)


def test_bot_lista_feiras_do_dia(tmp_path):
    rt = _rt(tmp_path)
    try:
        texto = _say(rt, "/feiras sabado")
        assert "Feiras em sabado" in texto
        assert "São Joaquim" in texto
        assert "/feira sao-joaquim" in texto
        assert "Segunda" not in texto.split("Feiras em sabado")[1].split("\n")[0]
    finally:
        rt.conn.close()


def test_bot_segunda_diz_que_nao_ha_dia_publicado(tmp_path):
    rt = _rt(tmp_path)
    try:
        texto = _say(rt, "/feiras segunda")
        assert "nenhuma feira" in texto.lower()
        assert "não são inferidas" in texto
    finally:
        rt.conn.close()


def test_bot_ficha_mostra_fonte(tmp_path):
    rt = _rt(tmp_path)
    try:
        texto = _say(rt, "/feira sao-joaquim")
        assert "Trecho" not in texto
        assert "SEMOP" in texto
        assert "7.500" in texto
        assert "https://" in texto
        assert "terca, quarta" in texto
    finally:
        rt.conn.close()


def test_bot_busca_por_bairro(tmp_path):
    rt = _rt(tmp_path)
    try:
        texto = _say(rt, "/feiras bairro Liberdade")
        assert "Japão / Liberdade" in texto
    finally:
        rt.conn.close()


def test_bot_sem_catalogo_nao_quebra(tmp_path):
    from bot.config import Settings

    settings = Settings(
        db_path=str(tmp_path / "vazio.db"), bot_token=None, target_chat_id=-1,
        allow_execute=False, max_per_run=3, min_interval_seconds=300,
        admin_ids=(9001,), api_base="https://api.telegram.org",
    )
    rt = build_runtime(settings)
    try:
        texto = _say(rt, "/feiras")
        assert "nenhuma feira" in texto.lower()
    finally:
        rt.conn.close()


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------
def test_cli_listar_json(tmp_path, capsys):
    from feiras import cli

    db = tmp_path / "feiras.db"
    assert cli.main(["--db", str(db), "carregar"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(db), "listar", "--dia", "sabado", "--json"]) == 0
    saida = capsys.readouterr().out
    assert '"slug": "sao-joaquim"' in saida
    assert saida.count('"slug"') == 23
