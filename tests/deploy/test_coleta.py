"""Rotina de coleta em lote: wiring, isolamento de falha e persistência.

Nada aqui toca a rede: o coletor é substituído por um stub. O que se verifica
é o que o agendador realmente executa — argv, código de saída, gravação sempre
como pendente e isolamento por fonte.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import REPO, run

sys.path.insert(0, str(REPO))

from collectors.fetch import ErroColeta  # noqa: E402
from collectors.models import EventoColetado  # noqa: E402
from sources.catalog import por_id  # noqa: E402

import scripts.coleta as coleta  # noqa: E402


def evento(fonte_id: str, titulo: str = "Show de teste") -> EventoColetado:
    return EventoColetado(
        fonte_id=fonte_id,
        titulo=titulo,
        inicio_em=datetime(2026, 12, 1, 20, 0, tzinfo=ZoneInfo("America/Bahia")),
        url_original="https://exemplo.invalid/evento",
        metodo_extracao="json_embutido",
        confianca="alta",
        trecho="trecho de evidência",
        local_nome="Teatro de teste",
    )


class ColetorStub:
    def __init__(self, fonte, eventos=None, erro=None):
        self.fonte = fonte
        self._eventos = eventos or []
        self._erro = erro
        self.ultimo_estado = {"stub": True}

    def coletar(self, limite=None):
        if self._erro:
            raise self._erro
        return self._eventos[:limite] if limite else self._eventos


def stub_registry(monkeypatch, por_fonte: dict):
    def fake_obter_coletor(fonte, fetcher=None):
        return por_fonte[fonte.id]

    monkeypatch.setattr(coleta, "obter_coletor", fake_obter_coletor)


def test_coleta_grava_sempre_pendente(monkeypatch, tmp_path, capsys):
    fonte = por_id("tca")
    stub_registry(monkeypatch, {fonte.id: ColetorStub(fonte, [evento(fonte.id)])})
    db = tmp_path / "agenda.db"

    code = coleta.main(["--fontes", fonte.id, "--gravar", "--db", str(db), "--json"])
    assert code == 0
    relatorio = json.loads(capsys.readouterr().out)
    assert relatorio["gravado"] is True
    assert relatorio["fontes_ok"] == 1
    assert relatorio["total_eventos"] == 1
    assert relatorio["aceitos"] == 1

    import sqlite3  # noqa: PLC0415

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        status = [r["status"] for r in conn.execute("SELECT status FROM eventos")]
        assert status == ["pendente"], "coleta não pode criar evento aprovado"
    finally:
        conn.close()


def test_coleta_sem_gravar_nao_cria_banco(monkeypatch, tmp_path, capsys):
    fonte = por_id("tca")
    stub_registry(monkeypatch, {fonte.id: ColetorStub(fonte, [evento(fonte.id)])})
    db = tmp_path / "nao-deve-existir.db"

    code = coleta.main(["--fontes", fonte.id, "--db", str(db), "--json"])
    assert code == 0
    relatorio = json.loads(capsys.readouterr().out)
    assert relatorio["gravado"] is False
    assert relatorio["fontes"][0]["persistencia"] is None
    assert not db.exists()


def test_falha_de_uma_fonte_nao_interrompe_as_outras(monkeypatch, capsys):
    tca = por_id("tca")
    sympla = por_id("sympla_salvador")
    stub_registry(monkeypatch, {
        tca.id: ColetorStub(tca, erro=ErroColeta("rede fora", fonte_id=tca.id)),
        sympla.id: ColetorStub(sympla, [evento(sympla.id, "Peça de teste")]),
    })

    code = coleta.main(["--fontes", f"{tca.id},{sympla.id}", "--json"])
    assert code == 0, "sucesso parcial é 0: fonte bloqueada não é bug do job"
    relatorio = json.loads(capsys.readouterr().out)
    assert relatorio["fontes_ok"] == 1
    assert relatorio["fontes_com_falha"] == 1
    falha = [f for f in relatorio["fontes"] if not f["ok"]][0]
    assert falha["fonte"] == tca.id and "rede fora" in falha["erro"]


def test_todas_as_fontes_falhando_sai_com_1(monkeypatch, capsys):
    tca = por_id("tca")
    stub_registry(monkeypatch, {
        tca.id: ColetorStub(tca, erro=ErroColeta("timeout", fonte_id=tca.id)),
    })
    assert coleta.main(["--fontes", tca.id, "--json"]) == 1


def test_fonte_desconhecida_sai_com_2(capsys):
    assert coleta.main(["--fontes", "nao_existe", "--json"]) == 2
    assert "fonte desconhecida" in capsys.readouterr().err


def test_limite_negativo_sai_com_2(capsys):
    assert coleta.main(["--limite", "-1", "--json"]) == 2
    assert "limite" in capsys.readouterr().err


def test_fontes_padrao_sao_apenas_coletaveis():
    padrao = coleta.fontes_disponiveis()
    assert padrao, "catálogo sem fonte coletável"
    assert all(f.coletavel for f in padrao)
    assert all(f.acessibilidade == "ok" for f in padrao)


def test_cli_ajuda_nao_quebra():
    proc = run(["scripts/coleta.py", "--help"])
    assert proc.returncode == 0
    assert "--gravar" in proc.stdout


@pytest.mark.parametrize("nome", ["tca", "sympla_salvador"])
def test_argv_do_agendador_chama_a_rotina(nome):
    from scripts.scheduler import TASKS  # noqa: PLC0415

    argv = TASKS["coleta"]({"fontes": [nome], "limite": 10, "gravar": True})
    assert argv[0] == sys.executable
    assert argv[1].endswith("scripts/coleta.py")
    assert argv[argv.index("--fontes") + 1] == nome
