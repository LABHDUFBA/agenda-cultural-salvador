"""Testes offline dos coletores de plataformas de ingresso com dado real.

Cobre os três coletores implementados em 2026-11-03:

* ``sympla_salvador`` — payload de hidratação publicado na listagem;
* ``casa_rosa`` — iframe público do Sympla embutido na página da casa;
* ``bilheteria_digital_salvador`` — cartões da busca + campos ocultos da
  página de detalhe.

Nenhum teste toca a rede: as páginas estão em ``tests/fixtures/`` como
recortes reais e datados (ver o comentário no topo de cada arquivo) e o
transporte HTTP é substituído por dublês. O que se verifica é o contrato:
proveniência, fuso, o que é dado e o que é inferência, e o que acontece
quando a fonte muda de layout ou bloqueia.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from collectors import htmldom
from collectors.bilheteria_digital import (
    ColetorBilheteriaDigital,
    extrair_cartoes as bd_cartoes,
    extrair_detalhe as bd_detalhe,
)
from collectors.casa_rosa import ColetorCasaRosa
from collectors.casa_rosa import encontrar_widget, extrair_cartoes as cr_cartoes
from collectors.fetch import (
    FonteBloqueada,
    FonteIndisponivel,
    FonteNaoSuportada,
    PaginaBaixada,
)
from collectors.models import METODOS
from collectors.registry import REGISTRO, obter_por_id
from collectors.sympla import (
    ColetorSympla,
    eventos_do_payload,
    extrair_eventos,
    objetos_json,
    payload_flight,
)
from sources.catalog import por_id

FIXTURES = Path(__file__).parent / "fixtures"
FUSO = ZoneInfo("America/Bahia")


def fixture(nome: str) -> str:
    return (FIXTURES / nome).read_text(encoding="utf-8")


def pagina(url: str, texto: str) -> PaginaBaixada:
    return PaginaBaixada(
        url_pedida=url,
        url_final=url,
        status=200,
        content_type="text/html; charset=utf-8",
        texto=texto,
        tamanho=len(texto.encode("utf-8")),
        buscado_em=datetime.now(FUSO),
        duracao_s=0.01,
        robots="permitido",
    )


class FetcherDuble:
    """Serve páginas por URL e registra a ordem das requisições."""

    def __init__(self, paginas: dict[str, str], falhas: dict[str, Exception] | None = None) -> None:
        self.paginas = paginas
        self.falhas = falhas or {}
        self.pedidos: list[str] = []

    def baixar(self, url: str, *, fonte_id: str = "") -> PaginaBaixada:
        self.pedidos.append(url)
        if url in self.falhas:
            raise self.falhas[url]
        if url not in self.paginas:
            raise AssertionError(f"requisição inesperada: {url}")
        return pagina(url, self.paginas[url])

    def sondar(self, url: str, *, fonte_id: str = ""):  # pragma: no cover - não usado
        raise AssertionError("estes coletores não sondam")


# ---------------------------------------------------------------------------
# Sympla
# ---------------------------------------------------------------------------
class TestSymplaPayload:
    def test_payload_flight_e_concatenado(self):
        payload = payload_flight(fixture("sympla_salvador.html"))
        assert payload.startswith("2:")
        assert '"start_date"' in payload

    def test_objetos_json_recorta_objetos_completos(self):
        objetos = list(objetos_json(payload_flight(fixture("sympla_salvador.html"))))
        assert objetos
        assert all("start_date" in objeto for objeto in objetos)
        assert all(isinstance(objeto.get("location"), dict) for objeto in objetos)

    def test_chunk_truncado_nao_derruba_o_parser(self):
        html = (
            '<script>self.__next_f.push([1,"2:{\\"events\\":[{\\"name\\":\\"A\\",'
            '\\"url\\":\\"https://bileto.sympla.com.br/event/9\\",'
            '\\"start_date\\":\\"2026-11-06T23:00:00+00:00\\"}]}"])</script>'
            '<script>self.__next_f.push([1,"3:{\\"trunc'
        )
        payload = payload_flight(html)
        assert '"start_date"' in payload
        assert len(list(objetos_json(payload))) == 1

    def test_extrai_salvador_com_fuso_e_sem_inferencia(self):
        itens, descartados = extrair_eventos(fixture("sympla_salvador.html"))
        assert len(itens) == 3
        assert descartados == 0
        item = itens[0]
        assert item["municipio"] == "Salvador" and item["uf"] == "BA"
        assert item["inicio"].utcoffset().total_seconds() == -3 * 3600
        assert item["legenda_data"], "legenda publicada pelo Sympla deve ir ao trecho"

    def test_deduplica_por_url_canonica(self):
        payload = (
            '{"events":[{"name":"A","url":"https://bileto.sympla.com.br/event/1",'
            '"start_date":"2026-11-06T23:00:00+00:00","location":{"city":"Salvador","state":"BA"}},'
            '{"name":"A","url":"https://bileto.sympla.com.br/event/1",'
            '"start_date":"2026-11-06T23:00:00+00:00","location":{"city":"Salvador","state":"BA"}}]}'
        )
        assert len(eventos_do_payload(payload)) == 1

    def test_evento_de_outra_cidade_e_descartado(self):
        payload = (
            '{"events":[{"name":"Fora","url":"https://bileto.sympla.com.br/event/2",'
            '"start_date":"2026-11-06T23:00:00+00:00","location":{"city":"Recife","state":"PE"}}]}'
        )
        html = (
            '<script>self.__next_f.push([1,%s])</script>' % _literal(payload)
        )
        itens, descartados = extrair_eventos(html)
        assert itens == [] and descartados == 1

    def test_sem_payload_falha_alto(self):
        with pytest.raises(FonteNaoSuportada, match="payload"):
            extrair_eventos("<html><body>nada aqui</body></html>")

    def test_coletor_monta_eventos_com_proveniencia(self):
        fetcher = FetcherDuble({"https://www.sympla.com.br/eventos/salvador-ba": fixture("sympla_salvador.html")})
        coletor = ColetorSympla(por_id("sympla_salvador"), fetcher)
        eventos = coletor.coletar()
        assert len(eventos) == 3
        assert fetcher.pedidos == ["https://www.sympla.com.br/eventos/salvador-ba"]
        evento = eventos[0]
        assert evento.metodo_extracao == "json_embutido"
        assert evento.confianca == "alta"
        assert evento.campos_inferidos == ()
        assert not evento.revisao_necessaria
        assert evento.url_original.startswith("https://bileto.sympla.com.br/event/")
        assert evento.municipio == "Salvador" and evento.uf == "BA"
        assert evento.trecho and evento.local_nome
        assert coletor.ultimo_estado["eventos"] == 3

    def test_limite_e_ordenacao_do_contrato(self):
        fetcher = FetcherDuble({"https://www.sympla.com.br/eventos/salvador-ba": fixture("sympla_salvador.html")})
        eventos = ColetorSympla(por_id("sympla_salvador"), fetcher).coletar(limite=2)
        assert len(eventos) == 2
        assert eventos[0].inicio_em <= eventos[1].inicio_em


# ---------------------------------------------------------------------------
# Casa Rosa
# ---------------------------------------------------------------------------
class TestCasaRosa:
    def test_encontra_o_iframe_publico_de_agenda(self):
        url = encontrar_widget(fixture("casa_rosa.html"), por_id("casa_rosa").url)
        assert url.startswith("https://www.sympla.com.br/agenda-eventos/")
        assert "theme=light" in url

    def test_pagina_sem_widget_nao_inventa_evento(self):
        fetcher = FetcherDuble({"https://site.bileto.sympla.com.br/casarosa/": "<html><body>só institucional</body></html>"})
        with pytest.raises(FonteNaoSuportada, match="widget"):
            ColetorCasaRosa(por_id("casa_rosa"), fetcher).coletar()

    def test_cartoes_do_widget(self):
        itens, descartados = cr_cartoes(fixture("casa_rosa_widget.html"), por_id("casa_rosa").url)
        assert len(itens) == 5 and descartados == 0
        primeiro = itens[0]
        assert primeiro["titulo"] == "Quinta da Casa – NIQUIM, de Guias Cegos"
        assert primeiro["dia"] == "5" and primeiro["mes"] == "Nov"
        assert primeiro["hora"] == "20:00"
        assert primeiro["url"].startswith("https://bileto.sympla.com.br/event/")

    def test_coletor_segue_o_iframe_e_marca_ano_inferido(self):
        pagina_casa = fixture("casa_rosa.html")
        url_widget = encontrar_widget(pagina_casa, por_id("casa_rosa").url)
        fetcher = FetcherDuble(
            {
                "https://site.bileto.sympla.com.br/casarosa/": pagina_casa,
                url_widget: fixture("casa_rosa_widget.html"),
            }
        )
        coletor = ColetorCasaRosa(por_id("casa_rosa"), fetcher)
        eventos = coletor.coletar()
        assert len(eventos) == 5
        assert fetcher.pedidos == [por_id("casa_rosa").url, url_widget]
        for evento in eventos:
            assert evento.inicio_em.tzinfo is not None
            assert evento.inicio_em.hour != 0, "hora é publicada pelo widget, não inferida"
            assert evento.campos_inferidos == ("ano",)
            assert evento.revisao_necessaria
            assert evento.confianca == "media"
            assert evento.local_nome == "Casa Rosa"
        assert coletor.ultimo_estado["anos_inferidos"] == 5


# ---------------------------------------------------------------------------
# Bilheteria Digital
# ---------------------------------------------------------------------------
class TestBilheteriaDigital:
    def test_cartoes_da_busca_filtram_cidade_publicada(self):
        itens, descartados = bd_cartoes(fixture("bd_busca.html"), por_id("bilheteria_digital_salvador").url)
        assert len(itens) == 3
        assert descartados == 1  # Ituberá - BA: descartado, não reinterpretado
        assert {i["cidade"] for i in itens} == {"Salvador - BA"}
        assert itens[0]["data"] == "14 de Novembro"

    def test_detalhe_le_data_iso_e_preco(self):
        dados = bd_detalhe(fixture("bd_evento.html"))
        assert dados["titulo"] == "ENSAIO DE NARCIZINHO"
        assert dados["local"] == "ESPAÇO CULTURAL DA BARROQUINHA"
        assert dados["data_bruta"] == "2026-11-15T20:00:00.000Z"
        inicio = dados["inicio"]
        assert inicio.isoformat() == "2026-11-15T17:00:00-03:00"
        assert dados["preco"] == "R$ 50,00"

    def _fetcher(self, falhas=None):
        base = "https://www.bilheteriadigital.com"
        paginas = {"https://www.bilheteriadigital.com/busca/salvador": fixture("bd_busca.html")}
        for slug in ("wine-nights-experience-14-de-novembro", "ensaio-de-narcizinho-15-de-novembro",
                     "reveillon-vira-salvador-2027"):
            paginas[f"{base}/{slug}"] = fixture("bd_evento.html")
        return FetcherDuble(paginas, falhas)

    def test_coletor_usa_detalhe_e_nao_infere(self):
        fetcher = self._fetcher()
        coletor = ColetorBilheteriaDigital(por_id("bilheteria_digital_salvador"), fetcher)
        eventos = coletor.coletar()
        assert len(eventos) == 3
        assert coletor.ultimo_estado["descartados"] == 1
        assert coletor.ultimo_estado["detalhes_consultados"] == 3
        for evento in eventos:
            assert evento.metodo_extracao == "atributo_html"
            assert evento.confianca == "alta"
            assert evento.campos_inferidos == ()
            assert evento.preco.startswith("R$ ")
            assert evento.inicio_em.utcoffset().total_seconds() == -3 * 3600

    def test_falha_no_detalhe_cai_para_cartao_com_inferencia_declarada(self):
        falhas = {
            "https://www.bilheteriadigital.com/ensaio-de-narcizinho-15-de-novembro": FonteIndisponivel(
                "timeout", fonte_id="bilheteria_digital_salvador"
            )
        }
        coletor = ColetorBilheteriaDigital(por_id("bilheteria_digital_salvador"), self._fetcher(falhas))
        eventos = coletor.coletar()
        assert len(eventos) == 3
        assert coletor.ultimo_estado["falhas_detalhe"] == 1
        caiu = [e for e in eventos if e.titulo == "ENSAIO DE NARCIZINHO"][0]
        assert caiu.metodo_extracao == "cartao_html"
        assert caiu.confianca == "baixa"
        assert set(caiu.campos_inferidos) == {"ano", "hora"}
        assert caiu.revisao_necessaria

    def test_bloqueio_no_detalhe_nao_e_contornado(self):
        falhas = {
            "https://www.bilheteriadigital.com/wine-nights-experience-14-de-novembro": FonteBloqueada(
                "HTTP 429", status=429, fonte_id="bilheteria_digital_salvador"
            )
        }
        coletor = ColetorBilheteriaDigital(por_id("bilheteria_digital_salvador"), self._fetcher(falhas))
        with pytest.raises(FonteBloqueada):
            coletor.coletar()

    def test_teto_de_detalhes_limita_requisicoes(self):
        coletor = ColetorBilheteriaDigital(
            por_id("bilheteria_digital_salvador"), self._fetcher(), max_detalhes=2
        )
        eventos = coletor.coletar()
        assert len(eventos) == 2
        assert coletor.ultimo_estado["detalhes_consultados"] == 2


# ---------------------------------------------------------------------------
# Integração com catálogo e registro
# ---------------------------------------------------------------------------
class TestIntegracaoComCatalogo:
    @pytest.mark.parametrize(
        "fonte_id,classe",
        [
            ("sympla_salvador", "sympla_payload"),
            ("casa_rosa", "casa_rosa_widget"),
            ("bilheteria_digital_salvador", "bilheteria_digital_cartoes"),
        ],
    )
    def test_catalogo_aponta_para_coletor_real(self, fonte_id, classe):
        fonte = por_id(fonte_id)
        assert fonte.coletor == classe
        assert fonte.coletor in REGISTRO
        assert fonte.acessibilidade == "ok"
        assert fonte.coletavel
        assert fonte.http_status == 200

    def test_coletor_registrado_e_instanciavel_sem_rede(self):
        for fonte_id in ("sympla_salvador", "casa_rosa", "bilheteria_digital_salvador"):
            coletor = obter_por_id(fonte_id, FetcherDuble({}))
            assert coletor.fonte.id == fonte_id

    def test_metodo_atributo_html_foi_declarado_no_contrato(self):
        assert "atributo_html" in METODOS

    def test_fixtures_nao_tem_dado_pessoal_obvio(self):
        import re

        for nome in ("sympla_salvador.html", "casa_rosa.html", "casa_rosa_widget.html",
                     "bd_busca.html", "bd_evento.html"):
            texto = fixture(nome)
            assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", texto), nome
            assert "hp_token" not in texto, nome
            assert "SessaoVendaId" not in texto, nome


def _literal(texto: str) -> str:
    import json

    return json.dumps(texto, ensure_ascii=False)
