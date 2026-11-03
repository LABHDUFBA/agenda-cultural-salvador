"""Testes do núcleo de dados da agenda cultural de Salvador."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database.repository as repository
from database import (
    DadoInvalido,
    ErroBanco,
    Evento,
    Fonte,
    Proveniencia,
    SCHEMA_VERSION,
    TransicaoInvalida,
    abrir_banco,
    aprovar_evento,
    chave_deduplicacao,
    finalizar_sessao,
    ingerir_evento,
    inicializar,
    iniciar_sessao,
    listar_auditoria,
    listar_pendentes,
    normalizar_texto,
    obter_evento,
    registrar_fonte,
    rejeitar_evento,
    validar_evento,
)

AGORA = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
HASH_A = hashlib.sha256(b"conteudo-a").hexdigest()
HASH_B = hashlib.sha256(b"conteudo-b").hexdigest()


def futuro(dias: int = 10, hora: int = 20) -> datetime:
    base = AGORA + timedelta(days=dias)
    return base.replace(hour=hora, minute=0, second=0, microsecond=0)


def novo_evento(**campos) -> Evento:
    base = {
        "titulo": "Concerto da OSBA",
        "inicio_em": futuro(),
        "municipio": "Salvador",
        "uf": "BA",
        "local_nome": "Teatro Castro Alves",
    }
    base.update(campos)
    return Evento(**base)


def nova_prov(fonte_id: int, sessao_id: int, **campos) -> Proveniencia:
    base = {
        "fonte_id": fonte_id,
        "sessao_id": sessao_id,
        "url_original": "https://exemplo.com/eventos/1",
        "hash_conteudo": HASH_A,
        "capturado_em": AGORA,
    }
    base.update(campos)
    return Proveniencia(**base)


@pytest.fixture
def conn(tmp_path):
    conexao = abrir_banco(tmp_path / "agenda.db")
    yield conexao
    conexao.close()


@pytest.fixture
def cenario(conn):
    fonte_id = registrar_fonte(
        conn, Fonte(nome="Portal Cultural", url_base="https://exemplo.com")
    )
    sessao_id = iniciar_sessao(conn, fonte_id)
    return conn, fonte_id, sessao_id


def ingerir(conn, fonte_id, sessao_id, evento=None, **prov_campos):
    return ingerir_evento(
        conn,
        evento or novo_evento(),
        nova_prov(fonte_id, sessao_id, **prov_campos),
        agora=AGORA,
    )


# --- schema e conexão -------------------------------------------------------


def test_schema_idempotente_e_versionado(conn):
    assert inicializar(conn) == SCHEMA_VERSION
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tabelas = {
        linha["name"]
        for linha in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "fontes", "sessoes", "eventos", "proveniencia", "auditoria",
        "usuarios", "tentativas_publicacao",
    } <= tabelas
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_recusa_schema_de_versao_futura(conn):
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(ErroBanco):
        inicializar(conn)


def test_banco_persistente_reabre_com_dados(tmp_path):
    caminho = tmp_path / "agenda.db"
    conn = abrir_banco(caminho)
    fonte_id = registrar_fonte(conn, Fonte(nome="F", url_base="https://f.com"))
    sessao_id = iniciar_sessao(conn, fonte_id)
    ingerir(conn, fonte_id, sessao_id)
    conn.close()
    conn2 = abrir_banco(caminho)
    assert len(listar_pendentes(conn2)) == 1
    conn2.close()


# --- fontes e sessões -------------------------------------------------------


def test_fonte_duplicada_recusada(conn):
    registrar_fonte(conn, Fonte(nome="Portal", url_base="https://a.com"))
    with pytest.raises(DadoInvalido):
        registrar_fonte(conn, Fonte(nome="Portal", url_base="https://b.com"))


def test_fonte_url_invalida_recusada(conn):
    with pytest.raises(DadoInvalido):
        registrar_fonte(conn, Fonte(nome="X", url_base="ftp://x.com"))


def test_sessao_inexistente_recusada(conn):
    with pytest.raises(DadoInvalido):
        iniciar_sessao(conn, 999)


def test_finalizar_sessao_uma_vez_so(cenario):
    conn, _, sessao_id = cenario
    finalizar_sessao(conn, sessao_id, status="concluida")
    linha = conn.execute("SELECT * FROM sessoes WHERE id = ?", (sessao_id,)).fetchone()
    assert linha["status"] == "concluida"
    assert linha["finalizada_em"] is not None
    with pytest.raises(TransicaoInvalida):
        finalizar_sessao(conn, sessao_id)


def test_sessao_finalizada_nao_aceita_ingestao(cenario):
    conn, fonte_id, sessao_id = cenario
    finalizar_sessao(conn, sessao_id)
    with pytest.raises(DadoInvalido):
        ingerir(conn, fonte_id, sessao_id)
    assert conn.execute("SELECT count(*) FROM eventos").fetchone()[0] == 0


def test_sessao_de_outra_fonte_recusada(cenario):
    conn, _, sessao_id = cenario
    outra = registrar_fonte(conn, Fonte(nome="Outra", url_base="https://outra.com"))
    with pytest.raises(DadoInvalido):
        ingerir(conn, outra, sessao_id, evento=novo_evento(titulo="Outro evento"))


def test_contadores_da_sessao(cenario):
    conn, fonte_id, sessao_id = cenario
    ingerir(conn, fonte_id, sessao_id)  # aceito
    ingerir(
        conn,
        fonte_id,
        sessao_id,
        evento=novo_evento(municipio="Camaçari"),
        url_original="https://exemplo.com/eventos/2",
    )  # rejeitado
    linha = conn.execute("SELECT * FROM sessoes WHERE id = ?", (sessao_id,)).fetchone()
    assert (linha["coletados"], linha["aceitos"], linha["rejeitados"]) == (2, 1, 1)


# --- validação de Salvador --------------------------------------------------


def test_evento_valido_entra_pendente_e_com_proveniencia(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id)
    assert resultado.status == "pendente"
    assert resultado.motivos == ()
    evento = obter_evento(conn, resultado.evento_id)
    assert evento["status"] == "pendente"
    assert evento["municipio"] == "Salvador"
    assert evento["data_ref"] == futuro().date().isoformat()
    assert len(evento["proveniencia"]) == 1
    prov = evento["proveniencia"][0]
    assert prov["url_original"] == "https://exemplo.com/eventos/1"
    assert prov["hash_conteudo"] == HASH_A
    assert prov["sessao_id"] == sessao_id


def test_rejeita_municipio_fora_de_salvador(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(
        conn, fonte_id, sessao_id, evento=novo_evento(municipio="Camaçari")
    )
    assert resultado.status == "rejeitado"
    assert any(m.startswith("fora_de_salvador") for m in resultado.motivos)
    evento = obter_evento(conn, resultado.evento_id)
    assert evento["status"] == "rejeitado"
    assert evento["chave_dedup"] is None
    assert "fora_de_salvador" in evento["motivo"]


def test_rejeita_uf_diferente(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id, evento=novo_evento(uf="SP"))
    assert resultado.status == "rejeitado"
    assert "uf_incompativel:SP" in resultado.motivos


def test_rejeita_sem_municipio_e_sem_coordenadas(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id, evento=novo_evento(municipio=""))
    assert resultado.status == "rejeitado"
    assert "salvador_nao_comprovado" in resultado.motivos


def test_pendente_quando_municipio_inferido_por_coordenadas(cenario):
    conn, fonte_id, sessao_id = cenario
    evento = novo_evento(municipio="", uf="BA", latitude=-12.99, longitude=-38.50)
    resultado = ingerir(conn, fonte_id, sessao_id, evento=evento)
    assert resultado.status == "pendente"
    assert "municipio_inferido_por_coordenadas" in resultado.motivos


def test_pendente_com_aviso_de_uf_ausente(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id, evento=novo_evento(uf=""))
    assert resultado.status == "pendente"
    assert resultado.motivos == ("uf_ausente",)


def test_rejeita_coordenadas_fora_da_caixa(cenario):
    conn, fonte_id, sessao_id = cenario
    evento = novo_evento(latitude=-23.55, longitude=-46.63)  # São Paulo
    resultado = ingerir(conn, fonte_id, sessao_id, evento=evento)
    assert resultado.status == "rejeitado"
    assert "coordenadas_fora_de_salvador" in resultado.motivos


def test_rejeita_coordenadas_incompletas(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(
        conn, fonte_id, sessao_id, evento=novo_evento(latitude=-12.99)
    )
    assert resultado.status == "rejeitado"
    assert "coordenadas_incompletas" in resultado.motivos


def test_rejeita_coordenadas_nao_numericas(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(
        conn, fonte_id, sessao_id, evento=novo_evento(latitude="12S", longitude="38W")
    )
    assert resultado.status == "rejeitado"
    assert "coordenadas_invalidas" in resultado.motivos


def test_rejeita_titulo_curto(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id, evento=novo_evento(titulo="AB"))
    assert resultado.status == "rejeitado"
    assert "titulo_invalido" in resultado.motivos


def test_rejeita_fim_antes_do_inicio(cenario):
    conn, fonte_id, sessao_id = cenario
    evento = novo_evento(fim_em=futuro() - timedelta(hours=2))
    resultado = ingerir(conn, fonte_id, sessao_id, evento=evento)
    assert resultado.status == "rejeitado"
    assert "fim_antes_do_inicio" in resultado.motivos


def test_rejeita_evento_encerrado(cenario):
    conn, fonte_id, sessao_id = cenario
    evento = novo_evento(inicio_em=AGORA - timedelta(days=5))
    resultado = ingerir(conn, fonte_id, sessao_id, evento=evento)
    assert resultado.status == "rejeitado"
    assert "evento_encerrado" in resultado.motivos


def test_rejeita_inicio_implausivel(cenario):
    conn, fonte_id, sessao_id = cenario
    evento = novo_evento(inicio_em=AGORA + timedelta(days=800))
    resultado = ingerir(conn, fonte_id, sessao_id, evento=evento)
    assert resultado.status == "rejeitado"
    assert "inicio_implausivel" in resultado.motivos


def test_rejeita_url_de_ingresso_invalida(cenario):
    conn, fonte_id, sessao_id = cenario
    evento = novo_evento(url_ingresso="javascript:alert(1)")
    resultado = ingerir(conn, fonte_id, sessao_id, evento=evento)
    assert resultado.status == "rejeitado"
    assert "url_ingresso_invalida" in resultado.motivos


def test_data_impossivel_vai_para_auditoria_sem_evento(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(
        conn, fonte_id, sessao_id, evento=novo_evento(inicio_em="31/12/2026")
    )
    assert resultado.status == "rejeitado"
    assert resultado.evento_id is None
    assert "inicio_invalido" in resultado.motivos
    assert conn.execute("SELECT count(*) FROM eventos").fetchone()[0] == 0
    auditoria = listar_auditoria(conn, entidade="evento")
    assert auditoria[-1]["acao"] == "ingestao_rejeitada"
    assert "31/12/2026" in auditoria[-1]["detalhe"]


def test_rejeicao_nao_bloqueia_evento_corrigido(cenario):
    conn, fonte_id, sessao_id = cenario
    ruim = ingerir(conn, fonte_id, sessao_id, evento=novo_evento(municipio="Lauro"))
    bom = ingerir(
        conn, fonte_id, sessao_id, url_original="https://exemplo.com/eventos/9"
    )
    assert ruim.status == "rejeitado"
    assert bom.status == "pendente"


def test_rejeita_proveniencia_sem_hash(cenario):
    conn, fonte_id, sessao_id = cenario
    with pytest.raises(DadoInvalido):
        ingerir(conn, fonte_id, sessao_id, hash_conteudo="")


def test_rejeita_proveniencia_hash_invalido(cenario):
    conn, fonte_id, sessao_id = cenario
    with pytest.raises(DadoInvalido):
        ingerir(conn, fonte_id, sessao_id, hash_conteudo="abc123")


def test_rejeita_proveniencia_url_invalida(cenario):
    conn, fonte_id, sessao_id = cenario
    with pytest.raises(DadoInvalido):
        ingerir(conn, fonte_id, sessao_id, url_original="exemplo.com/evento")


# --- deduplicação -----------------------------------------------------------


def test_dedup_exata_nao_cria_segundo_evento(cenario):
    conn, fonte_id, sessao_id = cenario
    primeiro = ingerir(conn, fonte_id, sessao_id)
    segundo = ingerir(
        conn, fonte_id, sessao_id, url_original="https://outro.com/evento/1"
    )
    assert segundo.status == "duplicado"
    assert segundo.duplicata_de == primeiro.evento_id
    assert conn.execute("SELECT count(*) FROM eventos").fetchone()[0] == 1
    evento = obter_evento(conn, primeiro.evento_id)
    assert len(evento["proveniencia"]) == 2
    linha = conn.execute("SELECT * FROM sessoes WHERE id = ?", (sessao_id,)).fetchone()
    assert (linha["coletados"], linha["aceitos"], linha["duplicados"]) == (2, 1, 1)
    acoes = [a["acao"] for a in listar_auditoria(conn, entidade="evento")]
    assert "duplicado" in acoes


def test_dedup_mesma_url_e_hash_nao_repete_proveniencia(cenario):
    conn, fonte_id, sessao_id = cenario
    primeiro = ingerir(conn, fonte_id, sessao_id)
    ingerir(conn, fonte_id, sessao_id)  # mesmo url + mesmo hash
    evento = obter_evento(conn, primeiro.evento_id)
    assert len(evento["proveniencia"]) == 1


def test_dedup_aproximada_fica_pendente_com_motivo(cenario):
    conn, fonte_id, sessao_id = cenario
    primeiro = ingerir(
        conn,
        fonte_id,
        sessao_id,
        evento=novo_evento(titulo="Festival de Jazz do Pelourinho"),
    )
    parecido = ingerir(
        conn,
        fonte_id,
        sessao_id,
        evento=novo_evento(titulo="Festival de Jazz do Pelourinho 2026"),
        url_original="https://outro.com/evento/2",
    )
    assert primeiro.status == "pendente"
    assert parecido.status == "pendente"
    assert parecido.evento_id != primeiro.evento_id
    assert f"possivel_duplicado:{primeiro.evento_id}" in parecido.motivos
    assert conn.execute("SELECT count(*) FROM eventos").fetchone()[0] == 2


def test_titulos_distintos_no_mesmo_dia_nao_sao_marcados(cenario):
    conn, fonte_id, sessao_id = cenario
    ingerir(conn, fonte_id, sessao_id)
    outro = ingerir(
        conn,
        fonte_id,
        sessao_id,
        evento=novo_evento(titulo="Feira de São Joaquim", local_nome="Cidade Baixa"),
        url_original="https://exemplo.com/eventos/3",
    )
    assert outro.status == "pendente"
    assert outro.motivos == ()


def test_chave_deduplicacao_ignora_acento_caixa_e_pontuacao():
    a = chave_deduplicacao(
        titulo="Concerto da OSBA: Beethoven",
        inicio_em=futuro(),
        local_nome="Teatro Castro Alves",
    )
    b = chave_deduplicacao(
        titulo="concerto da osba beethoven",
        inicio_em=futuro(),
        local_nome="teatro castro alves",
    )
    assert a == b
    assert normalizar_texto("  Ação  Cultural! ") == "acao cultural"


def test_chave_deduplicacao_distingue_data():
    a = chave_deduplicacao(titulo="X", inicio_em=futuro(10))
    b = chave_deduplicacao(titulo="X", inicio_em=futuro(11))
    assert a != b


# --- revisão e auditoria ----------------------------------------------------


def test_aprovacao_exige_status_pendente(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id)
    aprovar_evento(conn, resultado.evento_id, ator="curador", motivo="conferido")
    evento = obter_evento(conn, resultado.evento_id)
    assert evento["status"] == "aprovado"
    with pytest.raises(TransicaoInvalida):
        aprovar_evento(conn, resultado.evento_id, ator="curador")
    registro = listar_auditoria(conn, entidade="evento", entidade_id=resultado.evento_id)
    aprovacao = [a for a in registro if a["acao"] == "aprovado"][0]
    assert aprovacao["estado_anterior"] == "pendente"
    assert aprovacao["estado_novo"] == "aprovado"
    assert aprovacao["ator"] == "curador"


def test_aprovacao_exige_ator(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id)
    with pytest.raises(DadoInvalido):
        aprovar_evento(conn, resultado.evento_id, ator="  ")


def test_rejeicao_exige_motivo(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id)
    with pytest.raises(DadoInvalido):
        rejeitar_evento(conn, resultado.evento_id, ator="curador", motivo="")
    rejeitar_evento(conn, resultado.evento_id, ator="curador", motivo="data errada")
    assert obter_evento(conn, resultado.evento_id)["status"] == "rejeitado"


def test_transicao_de_rejeitado_nao_aprova(cenario):
    conn, fonte_id, sessao_id = cenario
    resultado = ingerir(conn, fonte_id, sessao_id, evento=novo_evento(municipio="Feira"))
    with pytest.raises(TransicaoInvalida):
        aprovar_evento(conn, resultado.evento_id, ator="curador")


def test_auditoria_e_append_only(conn):
    registrar_fonte(conn, Fonte(nome="F", url_base="https://f.com"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE auditoria SET ator = 'invasor'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM auditoria")


def test_listar_pendentes_ordenado_e_limitado(cenario):
    conn, fonte_id, sessao_id = cenario
    ingerir(
        conn,
        fonte_id,
        sessao_id,
        evento=novo_evento(titulo="Evento tarde", inicio_em=futuro(20)),
    )
    ingerir(
        conn,
        fonte_id,
        sessao_id,
        evento=novo_evento(titulo="Evento cedo", inicio_em=futuro(5)),
        url_original="https://exemplo.com/eventos/2",
    )
    pendentes = listar_pendentes(conn)
    assert [p["titulo"] for p in pendentes] == ["Evento cedo", "Evento tarde"]
    assert len(listar_pendentes(conn, limite=1)) == 1
    with pytest.raises(DadoInvalido):
        listar_pendentes(conn, limite=0)


def test_obter_evento_inexistente_devolve_none(conn):
    assert obter_evento(conn, 12345) is None


def test_falha_no_meio_da_ingestao_reverte_tudo(cenario, monkeypatch):
    conn, fonte_id, sessao_id = cenario
    auditoria_antes = conn.execute("SELECT count(*) FROM auditoria").fetchone()[0]

    def explodir(*args, **kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(repository, "_inserir_evento", explodir)
    with pytest.raises(RuntimeError):
        ingerir(conn, fonte_id, sessao_id)
    assert conn.execute("SELECT count(*) FROM eventos").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM proveniencia").fetchone()[0] == 0
    linha = conn.execute("SELECT * FROM sessoes WHERE id = ?", (sessao_id,)).fetchone()
    assert linha["coletados"] == 0
    assert (
        conn.execute("SELECT count(*) FROM auditoria").fetchone()[0]
        == auditoria_antes
    )


def test_validacao_pura_nao_toca_no_banco():
    decisao = validar_evento(novo_evento(), agora=AGORA)
    assert decisao.status == "pendente"
    assert decisao.rejeitado is False
    assert "municipio_salvador" in decisao.evidencias
