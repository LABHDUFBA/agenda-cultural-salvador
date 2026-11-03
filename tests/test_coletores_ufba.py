"""Testes offline das fontes UFBA e independentes (2026-11-03).

Cobre os três coletores novos e as duas fontes registradas sem parser:

* ``ufba_agenda`` — tabela Drupal de ``ufba.br/agenda-ufba``;
* ``ufba_escola_teatro`` — payload Next.js de ``teatro.ufba.br/agenda/``;
* ``cine_glauber_rocha`` — sessões de cinema publicadas no HTML;
* ``agenda_ufba_portal`` — TLS com cadeia incompleta, sem contorno;
* ``agenda_arte_cultura`` — agenda independente sem data de evento
  estruturada (registrada, não coletada).

Nenhum teste toca a rede: os fixtures são recortes reais e datados
(comentário no topo de cada arquivo) e o transporte é um dublê. O relógio é
injetado onde a filtragem de passados importa, para o teste não apodrecer.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from collectors.cine_glauber import ColetorCineGlauber, data_da_sessao, extrair_sessoes
from collectors.fetch import FonteBloqueada, PaginaBaixada
from collectors.registry import REGISTRO, obter_por_id
from collectors.ufba_agenda import ColetorAgendaUFBA, data_hora_publicada, extrair_agenda as ufba_linhas
from collectors.ufba_teatro import ColetorTeatroUFBA, extrair_agenda as teatro_itens
from sources.catalog import CATALOGO, TOTAL_FONTES, por_id, resumo

FIXTURES = Path(__file__).parent / "fixtures"
FUSO = ZoneInfo("America/Bahia")
NOVAS = ("ufba_agenda", "ufba_escola_teatro", "cine_glauber_rocha",
         "agenda_ufba_portal", "agenda_arte_cultura")


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
    def __init__(self, paginas: dict[str, str]) -> None:
        self.paginas = paginas
        self.pedidos: list[str] = []

    def baixar(self, url: str, *, fonte_id: str = "") -> PaginaBaixada:
        self.pedidos.append(url)
        if url not in self.paginas:
            raise AssertionError(f"requisição inesperada: {url}")
        return pagina(url, self.paginas[url])

    def sondar(self, url: str, *, fonte_id: str = ""):  # pragma: no cover
        raise AssertionError("estes coletores não sondam")


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------
class TestCatalogoAmpliado:
    def test_total_novo_e_unico(self):
        assert len(CATALOGO) == TOTAL_FONTES == 31
        assert len({f.id for f in CATALOGO}) == 31
        assert len({f.url for f in CATALOGO}) == 31

    def test_fontes_novas_estao_no_catalogo(self):
        for fonte_id in NOVAS:
            fonte = por_id(fonte_id)
            assert fonte.url.startswith("https://"), fonte_id
            assert fonte.coletor in REGISTRO, fonte_id
            assert fonte.observacao, fonte_id

    def test_coletaveis_sao_exatamente_as_com_parser(self):
        coletaveis = {f.id for f in CATALOGO if f.coletavel}
        for fonte_id in ("ufba_agenda", "ufba_escola_teatro", "cine_glauber_rocha"):
            assert fonte_id in coletaveis, fonte_id
        for fonte_id in ("agenda_ufba_portal", "agenda_arte_cultura"):
            assert fonte_id not in coletaveis, fonte_id

    def test_resumo_soma_31(self):
        contagem = resumo()
        assert sum(contagem.values()) == 31
        assert contagem["ok"] == len([f for f in CATALOGO if f.coletavel])


# ---------------------------------------------------------------------------
# Agenda UFBA (tabela Drupal)
# ---------------------------------------------------------------------------
class TestAgendaUFBA:
    def test_extrai_as_oito_linhas_reais(self):
        itens, ignorados = ufba_linhas(fixture("ufba_agenda.html"), "https://ufba.br/agenda-ufba")
        assert ignorados == 0
        assert len(itens) == 8
        primeiro = itens[0]
        assert primeiro["titulo"].startswith("Projeto de Preparação para Quaificação")
        assert primeiro["categoria"] == "Cursos"
        assert primeiro["local"].startswith("Sala de treinamento")
        assert primeiro["inicio"] == datetime(2026, 10, 26, 8, 8, tzinfo=FUSO)
        assert primeiro["fim"] == datetime(2026, 11, 30, 12, 12, tzinfo=FUSO)
        assert primeiro["url"] == (
            "https://ufba.br/agenda-ufba/projeto-de-preparacao-para-quaificacao-"
            "academica-propeq-modulo-iv-prova-escrita-em"
        )
        assert primeiro["descricao"].startswith("O Projeto de Preparação")

    def test_evento_de_um_dia_nao_tem_fim(self):
        itens, _ = ufba_linhas(fixture("ufba_agenda.html"), "https://ufba.br/agenda-ufba")
        oficina = next(i for i in itens if i["titulo"].startswith("Oficina: Introdução"))
        assert oficina["fim"] is None
        assert oficina["inicio"].hour == 14 and oficina["inicio"].minute == 2

    def test_todas_as_categorias_publicadas_aparecem(self):
        itens, _ = ufba_linhas(fixture("ufba_agenda.html"), "https://ufba.br/agenda-ufba")
        categorias = {i["categoria"] for i in itens}
        assert {"Cursos", "Eventos"} <= categorias

    def test_coletor_nao_infere_nada(self):
        url = "https://ufba.br/agenda-ufba"
        coletor = ColetorAgendaUFBA(por_id("ufba_agenda"), FetcherDuble({url: fixture("ufba_agenda.html")}),
                                    filtrar_passados=False)
        eventos = coletor.coletar()
        assert len(eventos) == 8
        assert all(e.campos_inferidos == () for e in eventos)
        assert all(e.confianca == "alta" for e in eventos)
        assert all(e.metodo_extracao == "atributo_html" for e in eventos)
        assert all(e.inicio_em.tzinfo is not None for e in eventos)
        assert eventos[0].categoria == "cursos"
        assert coletor.ultimo_estado["linhas"] == 8

    def test_filtro_de_passados_usa_o_fim_quando_existe(self):
        url = "https://ufba.br/agenda-ufba"
        dub = FetcherDuble({url: fixture("ufba_agenda.html")})
        # Em 15/12/2026 o primeiro evento (fim 30/11) já terminou; o de
        # 04/01/2027 continua. A contagem exata não importa: o que se prova
        # é que o corte usa fim/Início publicados.
        coletor = ColetorAgendaUFBA(por_id("ufba_agenda"), dub, agora=datetime(2026, 12, 15, tzinfo=FUSO))
        restantes = coletor.coletar()
        assert restantes
        assert all((e.fim_em or e.inicio_em) >= datetime(2026, 12, 14, tzinfo=FUSO) for e in restantes)
        assert all("Projeto de Preparação" not in e.titulo for e in restantes)

    @pytest.mark.parametrize(
        "texto,esperado",
        [
            ("26/10/2026 - 8h08", datetime(2026, 10, 26, 8, 8, tzinfo=FUSO)),
            ("04/11/2026 - 14h02", datetime(2026, 11, 4, 14, 2, tzinfo=FUSO)),
            ("31/02/2026 - 9h00", None),
            ("sem data", None),
            ("", None),
        ],
    )
    def test_data_hora_publicada(self, texto, esperado):
        assert data_hora_publicada(texto) == esperado

    def test_pagina_sem_tabela_devolve_vazio(self):
        html = "<html><body><h1>Agenda</h1><p>nada aqui</p></body></html>"
        itens, ignorados = ufba_linhas(html, "https://ufba.br/agenda-ufba")
        assert itens == [] and ignorados == 0

    def test_coletor_sem_tabela_nao_levanta(self):
        url = "https://ufba.br/agenda-ufba"
        coletor = ColetorAgendaUFBA(por_id("ufba_agenda"), FetcherDuble({url: "<html><body>mudou</body></html>"}))
        assert coletor.coletar() == []
        assert coletor.ultimo_estado["tabela_encontrada"] is False


# ---------------------------------------------------------------------------
# Escola de Teatro (payload Next.js)
# ---------------------------------------------------------------------------
class TestTeatroUFBA:
    def test_le_o_payload_e_converte_utc_para_bahia(self):
        itens = teatro_itens(fixture("ufba_teatro_agenda.html"), "https://teatro.ufba.br/agenda/")
        assert len(itens) == 3
        manutencao = next(i for i in itens if i["titulo"] == "Manutenção")
        # 2026-03-31T18:56:00+0000 == 15:56 em America/Bahia
        assert manutencao["inicio"] == datetime(2026, 3, 31, 15, 56, tzinfo=FUSO)
        assert manutencao["local"] == "Salvador UFBA"
        assert manutencao["categoria"] == "Eventos"
        assert manutencao["url"] == "https://teatro.ufba.br/agenda/manutencao-2024-2026quatro/"

    def test_tipos_multiplos_viram_categoria_textual(self):
        itens = teatro_itens(fixture("ufba_teatro_agenda.html"), "https://teatro.ufba.br/agenda/")
        assert all(isinstance(i["categoria"], str) for i in itens)
        assert itens[0]["categoria"] == "Espetáculos"

    def test_coletor_marca_confianca_alta_sem_inferencia(self):
        url = "https://teatro.ufba.br/agenda/"
        coletor = ColetorTeatroUFBA(por_id("ufba_escola_teatro"), FetcherDuble({url: fixture("ufba_teatro_agenda.html")}),
                                    filtrar_passados=False)
        eventos = coletor.coletar()
        assert len(eventos) == 3
        assert all(e.confianca == "alta" and e.campos_inferidos == () for e in eventos)
        assert all(e.metodo_extracao == "json_embutido" for e in eventos)
        assert coletor.ultimo_estado["registros"] == 3

    def test_filtro_de_passados_e_explicito(self):
        url = "https://teatro.ufba.br/agenda/"
        dub = FetcherDuble({url: fixture("ufba_teatro_agenda.html")})
        antigo = ColetorTeatroUFBA(por_id("ufba_escola_teatro"), dub, agora=datetime(2023, 1, 1, tzinfo=FUSO))
        assert len(antigo.coletar()) == 3
        futuro = ColetorTeatroUFBA(por_id("ufba_escola_teatro"), dub, agora=datetime(2030, 1, 1, tzinfo=FUSO))
        assert futuro.coletar() == []

    def test_payload_ausente_ou_truncado_devolve_vazio(self):
        assert teatro_itens("<html><body>sem next</body></html>", "https://teatro.ufba.br/agenda/") == []
        truncado = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"agenda":[{"uid":"x","title":"A"'
            "</script>"
        )
        assert teatro_itens(truncado, "https://teatro.ufba.br/agenda/") == []

    def test_data_sem_offset_e_descartada(self):
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props":{"pageProps":{"agenda":['
            '{"uid":"x","title":"Evento sem fuso","date":"2026-12-01T20:00:00","local":"UFBA"},'
            '{"uid":"y","title":"Evento com fuso","date":"2026-12-01T23:00:00+0000","local":"UFBA"}'
            "]}}}</script>"
        )
        itens = teatro_itens(html, "https://teatro.ufba.br/agenda/")
        assert [i["titulo"] for i in itens] == ["Evento com fuso"]
        assert itens[0]["inicio"] == datetime(2026, 12, 1, 20, 0, tzinfo=FUSO)


# ---------------------------------------------------------------------------
# Cine Glauber Rocha
# ---------------------------------------------------------------------------
class TestCineGlauber:
    def test_extrai_sessoes_dos_dois_filmes(self):
        itens, ignorados = extrair_sessoes(
            fixture("cine_glauber_programacao.html"),
            "https://www.cineglauberrocha.com.br/glauber-rocha/programacao",
        )
        assert ignorados == 0
        assert len(itens) == 3
        primeira = itens[0]
        assert primeira["titulo"] == "100 Dias"
        assert primeira["inicio"] == datetime(2026, 11, 3, 17, 10, tzinfo=FUSO)
        assert primeira["sala"] == "Sala 3"
        assert primeira["audio"] == "NAC"
        assert primeira["url_filme"].startswith("https://www.cineglauberrocha.com.br/glauber-rocha/filme/")
        assert primeira["url_ingresso"].startswith("https://cineglauberrocha.tikt.com.br/")
        assert "sessão de 03/11/2026 às 17:10" in primeira["trecho"]
        assert itens[2]["titulo"] == "Medley"

    def test_coletor_compone_local_com_sala(self):
        url = "https://www.cineglauberrocha.com.br/glauber-rocha/programacao"
        coletor = ColetorCineGlauber(por_id("cine_glauber_rocha"), FetcherDuble({url: fixture("cine_glauber_programacao.html")}),
                                     filtrar_passados=False)
        eventos = coletor.coletar()
        assert len(eventos) == 3
        primeiro = next(e for e in eventos if e.titulo == "100 Dias")
        assert primeiro.local_nome == "Cine Glauber Rocha — Sala 3"
        assert primeiro.categoria == "cinema"
        assert primeiro.confianca == "alta" and primeiro.campos_inferidos == ()
        assert primeiro.url_ingresso.startswith("https://")
        assert coletor.ultimo_estado["filmes"] == 2
        assert coletor.ultimo_estado["sessoes"] == 3

    def test_filtro_de_passados(self):
        url = "https://www.cineglauberrocha.com.br/glauber-rocha/programacao"
        dub = FetcherDuble({url: fixture("cine_glauber_programacao.html")})
        coletor = ColetorCineGlauber(por_id("cine_glauber_rocha"), dub, agora=datetime(2027, 1, 1, tzinfo=FUSO))
        assert coletor.coletar() == []

    @pytest.mark.parametrize(
        "texto,esperado",
        [
            ("ingresso para X, sessão de 03/11/2026 às 17:10, sala 3, NAC 2D",
             datetime(2026, 11, 3, 17, 10, tzinfo=FUSO)),
            ("sessao de 4/11/2026 as 9:05", datetime(2026, 11, 4, 9, 5, tzinfo=FUSO)),
            ("31/02/2026 às 10:00", None),
            ("sem sessão", None),
        ],
    )
    def test_data_da_sessao(self, texto, esperado):
        assert data_da_sessao(texto) == esperado

    def test_pagina_sem_filme_devolve_vazio(self):
        itens, ignorados = extrair_sessoes("<html><body><p>sem programação</p></body></html>", "https://x/")
        assert itens == [] and ignorados == 0


# ---------------------------------------------------------------------------
# Fontes registradas sem coleta
# ---------------------------------------------------------------------------
class TestFontesSemColeta:
    def test_portal_agenda_ufba_nao_faz_rede(self):
        class FetcherProibido:
            def baixar(self, url, *, fonte_id=""):  # pragma: no cover
                raise AssertionError("TLS inválido não deve ser contornado")

            def sondar(self, url, *, fonte_id=""):  # pragma: no cover
                raise AssertionError("não deveria sondar")

        coletor = obter_por_id("agenda_ufba_portal", FetcherProibido())
        with pytest.raises(FonteBloqueada):
            coletor.coletar()

    def test_agenda_arte_cultura_nao_e_coletavel(self):
        fonte = por_id("agenda_arte_cultura")
        assert fonte.acessibilidade == "nao_suportada"
        assert fonte.http_status == 200
        assert not fonte.coletavel
        assert "data de evento" in fonte.observacao.lower()
