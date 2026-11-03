"""Testes das 13 plataformas de ingresso obrigatórias no catálogo.

Escopo deste arquivo: (1) presença e integridade das 13 URLs exatas pedidas
pelo projeto, (2) honestidade do catálogo — nenhuma fonte sem coletor pode se
dizer coletável, (3) persistência do catálogo e do relatório de sondagem
(disco e fronteira de processo) e (4) acesso real, opcional e ético, com o
``HttpFetcher`` do projeto.

Nenhum teste padrão toca a rede. O teste de rede é pulado a menos que
``AGENDA_PROBE_REDE=1`` esteja definido; quando roda, faz **uma** requisição
por fonte, identifica o agente e respeita robots.txt (comportamento do
HttpFetcher, não deste arquivo).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from collectors.fetch import FonteBloqueada, HttpFetcher
from collectors.registry import REGISTRO, obter_por_id
from sources.catalog import CATALOGO, TOTAL_FONTES, fontes_coletaveis, por_id, resumo

RAIZ = Path(__file__).resolve().parent.parent
RELATORIO = RAIZ / "sources" / "probe_report_plataformas.json"

# Lista canônica exigida pelo projeto — literal, na ordem do pedido.
URLS_OBRIGATORIAS = {
    "shotgun_salvador": "https://shotgun.live/pt-br/cities/salvador",
    "shotgun_manifesto": "https://shotgun.live/pt-br/venues/manifesto",
    "ingresse": "https://ingresse.com/",
    "sympla_salvador": "https://www.sympla.com.br/eventos/salvador-ba",
    "casa_rosa": "https://site.bileto.sympla.com.br/casarosa/",
    "eventim_salvador": "https://www.eventim.com.br/city/salvador-1143/",
    "bilheteria_digital_salvador": "https://www.bilheteriadigital.com/busca/salvador",
    "ticketmaker": "https://www.ticketmaker.com.br/",
    "ticketmaker_eventos": "https://vendas.ticketmaker.com.br/Eventos",
    "ticketmaster_brasil": "https://www.ticketmaster.com.br/",
    "bora_tickets": "https://vendas.boratickets.com.br/",
    "pida": "https://www.pida.com.br/eventos",
    "nokta_salvador": "https://www.noktatickets.com.br/eventos/em/salvador",
}

NOVAS = tuple(URLS_OBRIGATORIAS)

# Implementadas em 2026-11-03, com dado estruturado publicado pela fonte.
IMPLEMENTADAS = ("sympla_salvador", "casa_rosa", "bilheteria_digital_salvador")
SEM_COLETOR = tuple(f for f in NOVAS if f not in IMPLEMENTADAS)


def _instantaneo() -> list[dict]:
    """Retrato serializável do catálogo (tuplas viram listas)."""
    return [
        json.loads(json.dumps(asdict(fonte), ensure_ascii=False, default=list))
        for fonte in CATALOGO
    ]


# ---------------------------------------------------------------------------
# 1. Presença exata das 13 fontes obrigatórias
# ---------------------------------------------------------------------------
class TestPresencaDasPlataformas:
    def test_catalogo_tem_31_fontes(self):
        assert len(CATALOGO) == TOTAL_FONTES == 31
        assert len(fontes_coletaveis()) <= len(CATALOGO)

    @pytest.mark.parametrize("fonte_id,url", sorted(URLS_OBRIGATORIAS.items()))
    def test_cada_url_obrigatoria_esta_no_catalogo(self, fonte_id, url):
        fonte = por_id(fonte_id)
        assert fonte.url == url

    def test_as_13_novas_fontes_sao_privadas_e_https(self):
        for fonte_id in NOVAS:
            fonte = por_id(fonte_id)
            assert fonte.url.startswith("https://"), fonte_id
            assert fonte.escopo == "privado", fonte_id
            assert "ingressos" in fonte.tags or "venue" in fonte.tags, fonte_id

    def test_nenhuma_fonte_antiga_foi_removida(self):
        antigas = {
            "pelourinho_dia_noite",
            "secult_salvador",
            "prefeitura_salvador",
            "tca",
            "funceb",
            "secult_ba",
            "ipac",
            "teatro_vila_velha",
            "salvador_da_bahia",
            "caixa_cultural_salvador",
            "mam_bahia",
            "muncab",
            "sesc_bahia",
        }
        assert antigas <= {f.id for f in CATALOGO}

    def test_ids_e_urls_continuam_unicos(self):
        assert len({f.id for f in CATALOGO}) == len(CATALOGO)
        assert len({f.url for f in CATALOGO}) == len(CATALOGO)


# ---------------------------------------------------------------------------
# 2. Honestidade: nada de coletor inventado
# ---------------------------------------------------------------------------
class TestHonestidadeDoCatalogo:
    def test_todo_coletor_declarado_existe_no_registro(self):
        for fonte in CATALOGO:
            assert fonte.coletor in REGISTRO, fonte.id

    def test_nenhuma_plataforma_nova_se_diz_coletavel(self):
        # As três implementadas são coletáveis de verdade; as demais continuam
        # sem coletor e não podem se declarar coletáveis.
        for fonte_id in SEM_COLETOR:
            fonte = por_id(fonte_id)
            assert not fonte.coletavel, fonte_id
            assert fonte.acessibilidade != "ok", fonte_id
            assert fonte.coletor in {"bloqueada", "nao_suportada"}, fonte_id

    def test_plataformas_implementadas_tem_coletor_registrado(self):
        for fonte_id in IMPLEMENTADAS:
            fonte = por_id(fonte_id)
            assert fonte.coletavel, fonte_id
            assert fonte.acessibilidade == "ok", fonte_id
            assert fonte.coletor in REGISTRO, fonte_id
            assert fonte.coletor not in {"bloqueada", "nao_suportada"}, fonte_id

    def test_nao_suportada_guarda_o_status_http_real(self):
        for fonte in CATALOGO:
            if fonte.acessibilidade == "nao_suportada":
                assert fonte.http_status == 200, fonte.id
                assert fonte.coletor == "nao_suportada", fonte.id

    def test_bloqueada_so_com_status_de_bloqueio(self):
        # Fontes com bloqueio de rede (sem resposta HTTP) ficam com
        # http_status None; as 13 plataformas novas recusaram com 403/429.
        for fonte in CATALOGO:
            if fonte.acessibilidade == "bloqueada" and fonte.http_status is not None:
                assert fonte.http_status in (401, 403, 429), fonte.id
        for fonte_id in NOVAS:
            fonte = por_id(fonte_id)
            if fonte.acessibilidade == "bloqueada":
                assert fonte.http_status in (403, 429), fonte_id

    def test_indisponivel_nao_inventa_status(self):
        for fonte in CATALOGO:
            if fonte.acessibilidade == "indisponivel":
                assert fonte.http_status is None, fonte.id

    def test_resumo_soma_o_catalogo(self):
        contagem = resumo()
        assert sum(contagem.values()) == 31
        assert contagem["ok"] == len(fontes_coletaveis())

    def test_coletor_de_fonte_bloqueada_nao_faz_rede(self):
        chamadas: list[str] = []

        class FetcherProibido:
            def sondar(self, url, *, fonte_id=""):  # pragma: no cover
                chamadas.append(url)
                raise AssertionError("não deveria sondar fonte bloqueada")

            def baixar(self, url, *, fonte_id=""):  # pragma: no cover
                chamadas.append(url)
                raise AssertionError("não deveria baixar fonte bloqueada")

        for fonte_id in ("ingresse", "ticketmaster_brasil", "shotgun_salvador"):
            coletor = obter_por_id(fonte_id, FetcherProibido())
            with pytest.raises(FonteBloqueada):
                coletor.coletar()
        assert chamadas == []


# ---------------------------------------------------------------------------
# 3. Persistência: catálogo e relatório sobrevivem ao disco e ao processo
# ---------------------------------------------------------------------------
class TestPersistencia:
    def test_catalogo_serializa_e_reidrata_igual(self, tmp_path: Path):
        original = _instantaneo()
        destino = tmp_path / "catalogo.json"
        destino.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")
        recarregado = json.loads(destino.read_text(encoding="utf-8"))
        assert recarregado == original
        assert len(recarregado) == 31
        assert {f["id"] for f in recarregado} == {f.id for f in CATALOGO}

    def test_novo_processo_enxerga_as_31_fontes(self):
        saida = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json;from sources.catalog import CATALOGO,TOTAL_FONTES;"
                "print(json.dumps({'n': len(CATALOGO), 'total': TOTAL_FONTES, "
                "'ids': [f.id for f in CATALOGO]}))",
            ],
            cwd=RAIZ,
            capture_output=True,
            text=True,
            check=True,
        )
        dados = json.loads(saida.stdout)
        assert dados["n"] == dados["total"] == 31
        assert set(NOVAS) <= set(dados["ids"])

    def test_relatorio_de_sondagem_persistido_e_coerente(self):
        dados = json.loads(RELATORIO.read_text(encoding="utf-8"))
        assert dados["gerado_em"].startswith("2026-11-03")
        assert dados["politica"]["retry"] == "nenhum"
        assert dados["politica"]["contorno"].startswith("nenhum")
        assert dados["resumo"]["total"] == 13
        assert len(dados["resultados"]) == 13
        assert {r["fonte_id"] for r in dados["resultados"]} == set(NOVAS)

    @pytest.mark.parametrize("fonte_id", sorted(URLS_OBRIGATORIAS))
    def test_catalogo_concorda_com_o_relatorio(self, fonte_id):
        relatorio = json.loads(RELATORIO.read_text(encoding="utf-8"))
        bruto = next(r for r in relatorio["resultados"] if r["fonte_id"] == fonte_id)
        fonte = por_id(fonte_id)
        assert bruto["url"] == fonte.url
        assert bruto["sondado_em"].startswith("2026-11-03")
        if "status" in bruto:
            assert fonte.http_status == bruto["status"]
        if bruto.get("erro") == "FonteIndisponivel":
            assert fonte.acessibilidade == "indisponivel"
        elif bruto.get("status") in (401, 403, 429) or bruto.get("erro") == "RobotsProibido":
            assert fonte.acessibilidade == "bloqueada"
        elif bruto.get("status") == 200:
            # Sondagem de 2026-11-03 apenas mediu acesso; se hoje há coletor,
            # a acessibilidade foi promovida a "ok" e o coletor é real.
            assert fonte.acessibilidade in {"nao_suportada", "ok"}
            if fonte.acessibilidade == "ok":
                assert fonte.coletor in REGISTRO
                assert fonte.coletor not in {"bloqueada", "nao_suportada"}


# ---------------------------------------------------------------------------
# 4. Acesso real (opt-in): uma requisição por fonte, sem contorno
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    os.environ.get("AGENDA_PROBE_REDE") != "1",
    reason="teste de rede real; habilite com AGENDA_PROBE_REDE=1",
)
class TestAcessoReal:
    def test_sondagem_real_classifica_todas_as_13(self):
        fetcher = HttpFetcher(timeout=20.0, intervalo_minimo=1.0)
        for fonte_id in NOVAS:
            fonte = por_id(fonte_id)
            resultado = fetcher.sondar(fonte.url, fonte_id=fonte_id)
            assert "acessivel" in resultado, fonte_id
            if resultado["acessivel"]:
                assert resultado["status"] == 200, fonte_id
                assert resultado["tamanho"] > 0, fonte_id
            else:
                assert resultado.get("erro"), fonte_id

    def test_agente_se_identifica(self):
        ua = HttpFetcher().user_agent
        assert "AgendaCulturalSalvadorBot" in ua
        assert "Mozilla" not in ua
