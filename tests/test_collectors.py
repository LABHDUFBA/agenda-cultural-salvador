"""Testes dos coletores e do catálogo de fontes.

Regra: nenhum teste toca a rede. As páginas reais estão em
``tests/fixtures/`` (recortes verdadeiros, com nomes e datas originais) e o
transporte HTTP é substituído por dublês.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from collectors import ErroColeta, FonteBloqueada, FonteNaoSuportada, HttpFetcher, RobotsProibido
from collectors import bagov, cli
from collectors.bagov import ColetorBaGovCalendario, extrair_calendario
from collectors.fetch import FonteIndisponivel, RespostaInvalida
from collectors.models import EventoColetado, limpar_texto
from collectors.nulls import ColetorBloqueada, ColetorNaoSuportada
from collectors.registry import REGISTRO, obter_coletor, obter_por_id
from collectors.salvador_da_bahia import ColetorSalvadorDaBahia, extrair_cartoes
from collectors.textutil import ano_provavel, com_hora, mes_de, url_absoluta
from collectors.vila_velha import ColetorVilaVelha, extrair_agenda
from sources.catalog import CATALOGO, TOTAL_FONTES, fontes_coletaveis, por_id, resumo

FIXTURES = Path(__file__).parent / "fixtures"
FUSO = ZoneInfo("America/Bahia")


def fixture(nome: str) -> str:
    return (FIXTURES / nome).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------
class TestCatalogo:
    def test_tem_exatamente_31_fontes(self):
        # 13 institucionais + 13 plataformas + 5 UFBA/independentes.
        assert len(CATALOGO) == TOTAL_FONTES == 31

    def test_ids_e_urls_sao_unicos(self):
        assert len({f.id for f in CATALOGO}) == 31
        assert len({f.url for f in CATALOGO}) == 31

    def test_somente_https(self):
        assert all(f.url.startswith("https://") for f in CATALOGO)

    def test_todo_coletor_esta_registrado(self):
        for fonte in CATALOGO:
            assert fonte.coletor in REGISTRO, fonte.id

    def test_campos_obrigatorios_preenchidos(self):
        for fonte in CATALOGO:
            assert fonte.nome and fonte.organizacao and fonte.observacao, fonte.id
            assert fonte.escopo in {"municipal", "estadual", "federal", "privado"}

    def test_bloqueadas_nao_sao_coletaveis(self):
        for fonte in CATALOGO:
            if fonte.acessibilidade in {"bloqueada", "indisponivel", "nao_suportada"}:
                assert not fonte.coletavel, fonte.id

    def test_resumo_bate_com_catalogo(self):
        contagem = resumo()
        assert sum(contagem.values()) == 31
        assert contagem["ok"] == len(fontes_coletaveis())

    def test_por_id_desconhecido_falha_com_mensagem(self):
        with pytest.raises(KeyError, match="fonte desconhecida"):
            por_id("nao_existe")

    def test_fontes_municipais_bloqueadas_estao_documentadas(self):
        for fonte_id in ("pelourinho_dia_noite", "secult_salvador", "prefeitura_salvador"):
            fonte = por_id(fonte_id)
            assert fonte.acessibilidade == "bloqueada"
            assert "timeout" in fonte.observacao.lower() or "tcp" in fonte.observacao.lower()


# ---------------------------------------------------------------------------
# Extração: calendário Drupal (ba.gov.br)
# ---------------------------------------------------------------------------
class TestCalendarioBaGov:
    def test_extrai_eventos_reais_do_fixture(self):
        eventos = extrair_calendario(fixture("tca_calendario.html"), "https://www.ba.gov.br/tca/eventos")
        assert len(eventos) == 3
        titulos = [e["title"] for e in eventos]
        assert "OSBA em Série Jorge Amado" in titulos
        assert all(e["start"].startswith("20") for e in eventos)

    def test_pagina_sem_calendario_devolve_vazio(self):
        html = "<html><body><h1>Notícias</h1><p>sem calendário</p></body></html>"
        assert extrair_calendario(html, "https://www.ba.gov.br/cultura/eventos") == []

    def test_json_truncado_nao_derruba_o_parser(self):
        html = '<script>{"eventTimeFormat":"HH:mm","events":[{"title":"A"'
        assert extrair_calendario(html, "https://x/") == []

    def test_coletor_converte_fuso_e_marca_confianca(self, stub_fetcher):
        stub_fetcher.paginas["https://www.ba.gov.br/tca/eventos"] = fixture("tca_calendario.html")
        coletor = obter_coletor(por_id("tca"), stub_fetcher)
        eventos = coletor.coletar()
        assert len(eventos) == 3
        primeiro = eventos[0]
        assert primeiro.inicio_em.tzinfo is not None
        # 19:00 em America/Bahia == 22:00 UTC
        assert primeiro.inicio_em.astimezone(timezone.utc).hour == 22
        assert primeiro.confianca == "alta"
        assert primeiro.campos_inferidos == ()
        assert not primeiro.revisao_necessaria
        assert primeiro.url_original.startswith("https://www.ba.gov.br/tca/eventos/")

    def test_filtra_eventos_passados_por_padrao(self, stub_fetcher):
        html = (
            '<script>{"eventTimeFormat":"HH:mm","events":['
            '{"title":"Antigo","start":"2020-01-01T19:00:00","url":"/tca/eventos/antigo"},'
            '{"title":"Futuro","start":"2099-01-01T19:00:00","url":"/tca/eventos/futuro"}]}'
            "</script>"
        )
        stub_fetcher.paginas["https://www.ba.gov.br/tca/eventos"] = html
        eventos = obter_coletor(por_id("tca"), stub_fetcher).coletar()
        assert [e.titulo for e in eventos] == ["Futuro"]

    def test_calendario_vazio_nao_e_erro(self, stub_fetcher):
        stub_fetcher.paginas["https://www.ba.gov.br/cultura/eventos"] = (
            '<script>{"eventTimeFormat":"HH:mm","events":[]}</script>'
        )
        coletor = obter_coletor(por_id("secult_ba"), stub_fetcher)
        assert coletor.coletar() == []
        assert coletor.ultimo_estado["calendario_encontrado"] is True


# ---------------------------------------------------------------------------
# Extração: cartões do Salvador da Bahia
# ---------------------------------------------------------------------------
class TestCartoesSalvadorDaBahia:
    def test_extrai_cartoes_e_descarta_orfao(self):
        itens, descartados = extrair_cartoes(
            fixture("sdb_agenda.html"), "https://www.salvadordabahia.com/agenda/"
        )
        assert descartados == 1  # cartão antes de qualquer cabeçalho de dia
        assert len(itens) == 2
        titulos = [i["titulo"] for i in itens]
        assert 'Exposição "Padê Onã – Encontrar Caminhos"' in titulos
        primeiro = itens[0]
        assert primeiro["ano"] == "2026" and primeiro["mes"] == "11" and primeiro["dia"] == "3"
        assert primeiro["local"].startswith("MUNCAB")
        assert primeiro["endereco"].startswith("Rua das Vassouras")

    def test_coletor_marca_hora_inferida_e_revisao(self, stub_fetcher):
        stub_fetcher.paginas["https://www.salvadordabahia.com/agenda/"] = fixture("sdb_agenda.html")
        coletor = obter_coletor(por_id("salvador_da_bahia"), stub_fetcher)
        eventos = coletor.coletar()
        assert len(eventos) == 2
        evento = eventos[0]
        assert evento.inicio_em.hour == 0 and evento.inicio_em.minute == 0
        assert evento.inicio_em.tzinfo is not None
        assert "hora" in evento.campos_inferidos
        assert evento.revisao_necessaria is True
        assert evento.metodo_extracao == "cartao_html"
        assert evento.municipio == "Salvador" and evento.uf == "BA"


# ---------------------------------------------------------------------------
# Extração: agenda em texto do Teatro Vila Velha
# ---------------------------------------------------------------------------
class TestAgendaVilaVelha:
    def test_extrai_itens_dos_dois_meses(self):
        itens, ignorados = extrair_agenda(
            fixture("tvv_agenda.html"), referencia=datetime(2026, 11, 3, tzinfo=FUSO)
        )
        assert ignorados == 0
        assert len(itens) == 5
        assert {(i["mes"], i["ano"]) for i in itens} == {(8, 2026), (9, 2026)}
        assert itens[0]["titulo"].startswith("PALCO ABERTO")
        assert itens[0]["local"] == "Teatro Gregório de Mattos"

    def test_intervalo_de_dias_vira_duas_sessoes(self):
        itens, _ = extrair_agenda(
            fixture("tvv_agenda.html"), referencia=datetime(2026, 11, 3, tzinfo=FUSO)
        )
        morrer = [i for i in itens if i["titulo"] == "MORRER PELOS PASSARINHOS"]
        assert sorted(i["dia"] for i in morrer) == [12, 13]

    def test_coletor_sempre_baixa_confianca(self, stub_fetcher):
        stub_fetcher.paginas["https://teatrovilavelha.com.br/tempo/"] = fixture("tvv_agenda.html")
        eventos = obter_coletor(por_id("teatro_vila_velha"), stub_fetcher).coletar()
        assert len(eventos) == 5
        assert all(e.confianca == "baixa" for e in eventos)
        assert all(e.revisao_necessaria for e in eventos)
        assert all(e.inicio_em.hour == 19 or e.inicio_em.hour == 16 for e in eventos)

    def test_local_eh_separado_da_observacao(self):
        html = (
            "<html><body><div class='qodef-qi-accordion'>"
            "<h1 class='qodef-e-title-holder'><span class='qodef-e-title'>janeiro/27</span></h1>"
            "<div class='qodef-e-content'><p><strong>ESPETÁCULO X</strong> - 10 de janeiro - 20h - "
            "Teatro Vila Velha (<em>ingressos em breve</em>)</p></div>"
            "</div></body></html>"
        )
        itens, _ = extrair_agenda(html, referencia=datetime(2026, 11, 3, tzinfo=FUSO))
        assert itens[0]["local"] == "Teatro Vila Velha"
        assert itens[0]["observacao"] == "ingressos em breve"


# ---------------------------------------------------------------------------
# Fontes bloqueadas / não suportadas
# ---------------------------------------------------------------------------
class TestFontesSemColeta:
    def test_fonte_bloqueada_levanta_sem_tocar_na_rede(self, stub_fetcher):
        coletor = obter_por_id("caixa_cultural_salvador", stub_fetcher)
        with pytest.raises(FonteBloqueada) as erro:
            coletor.coletar()
        assert erro.value.status == 403
        assert stub_fetcher.chamadas == []  # nenhuma requisição feita

    def test_nao_suportada_explica_motivo(self, stub_fetcher):
        coletor = obter_por_id("sesc_bahia", stub_fetcher)
        with pytest.raises(FonteNaoSuportada, match="API interna|SPA"):
            coletor.coletar()

    def test_bloqueio_de_rede_nao_insiste(self, stub_fetcher):
        coletor = obter_por_id("pelourinho_dia_noite", stub_fetcher)
        with pytest.raises(FonteBloqueada):
            coletor.coletar()
        assert stub_fetcher.chamadas == []


# ---------------------------------------------------------------------------
# Política HTTP
# ---------------------------------------------------------------------------
class RespostaFalsa:
    def __init__(self, corpo: bytes, *, status=200, content_type="text/html; charset=utf-8", url="https://exemplo.test/"):
        self.corpo = corpo
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._url = url

    def read(self, n: int = -1) -> bytes:
        return self.corpo if n < 0 else self.corpo[:n]

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestPoliticaHttp:
    def test_403_encerra_com_fonte_bloqueada(self, monkeypatch):
        chamadas = []

        def falso(pedido, timeout=None):
            chamadas.append(pedido)
            raise urllib.error.HTTPError(pedido.full_url, 403, "Forbidden", {}, io.BytesIO(b""))

        monkeypatch.setattr("collectors.fetch.urllib.request.urlopen", falso)
        fetcher = HttpFetcher(respeitar_robots=False)
        with pytest.raises(FonteBloqueada) as erro:
            fetcher.baixar("https://www.caixacultural.gov.br/", fonte_id="caixa")
        assert erro.value.status == 403
        assert len(chamadas) == 1  # sem retry, sem troca de identidade

    def test_429_tambem_e_bloqueio(self, monkeypatch):
        def falso(pedido, timeout=None):
            raise urllib.error.HTTPError(pedido.full_url, 429, "Too Many", {}, io.BytesIO(b""))

        monkeypatch.setattr("collectors.fetch.urllib.request.urlopen", falso)
        with pytest.raises(FonteBloqueada):
            HttpFetcher(respeitar_robots=False).baixar("https://x.test/a")

    def test_timeout_vira_indisponivel(self, monkeypatch):
        def falso(pedido, timeout=None):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr("collectors.fetch.urllib.request.urlopen", falso)
        with pytest.raises(FonteIndisponivel):
            HttpFetcher(respeitar_robots=False).baixar("https://www.salvador.ba.gov.br/")

    def test_robots_proibido_impede_download(self, monkeypatch):
        chamadas = []

        def falso(pedido, timeout=None):
            chamadas.append(pedido.full_url)
            if pedido.full_url.endswith("/robots.txt"):
                return RespostaFalsa(b"User-agent: *\nDisallow: /\n", content_type="text/plain")
            return RespostaFalsa(b"<html>nao deveria baixar</html>")

        monkeypatch.setattr("collectors.fetch.urllib.request.urlopen", falso)
        with pytest.raises(RobotsProibido):
            HttpFetcher().baixar("https://bloqueado.test/pagina")
        assert not any("pagina" in url for url in chamadas)

    def test_robots_ausente_permite(self, monkeypatch):
        def falso(pedido, timeout=None):
            if pedido.full_url.endswith("/robots.txt"):
                raise urllib.error.HTTPError(pedido.full_url, 404, "Not Found", {}, io.BytesIO(b""))
            return RespostaFalsa(b"<html><body>ok</body></html>")

        monkeypatch.setattr("collectors.fetch.urllib.request.urlopen", falso)
        pagina = HttpFetcher().baixar("https://sem-robots.test/x")
        assert pagina.robots == "ausente"
        assert "ok" in pagina.texto

    def test_content_type_inesperado_e_recusado(self, monkeypatch):
        monkeypatch.setattr(
            "collectors.fetch.urllib.request.urlopen",
            lambda pedido, timeout=None: RespostaFalsa(b"\x00\x01", content_type="application/pdf"),
        )
        with pytest.raises(RespostaInvalida, match="content-type"):
            HttpFetcher(respeitar_robots=False).baixar("https://x.test/a.pdf")

    def test_user_agent_se_identifica_e_nao_finge_navegador(self):
        ua = HttpFetcher().user_agent
        assert "AgendaCulturalSalvadorBot" in ua
        assert "Mozilla" not in ua
        assert not hasattr(HttpFetcher(), "user_agents")  # sem rotação

    def test_limite_de_tamanho_trunca(self, monkeypatch):
        grande = b"<html>" + b"a" * 5000 + b"</html>"
        monkeypatch.setattr(
            "collectors.fetch.urllib.request.urlopen",
            lambda pedido, timeout=None: RespostaFalsa(grande),
        )
        pagina = HttpFetcher(respeitar_robots=False, tamanho_maximo=1000).baixar("https://x.test/")
        assert pagina.tamanho == 1000

    def test_sondar_nao_levanta(self, monkeypatch):
        def falso(pedido, timeout=None):
            raise urllib.error.HTTPError(pedido.full_url, 403, "Forbidden", {}, io.BytesIO(b""))

        monkeypatch.setattr("collectors.fetch.urllib.request.urlopen", falso)
        resultado = HttpFetcher(respeitar_robots=False).sondar("https://x.test/")
        assert resultado["acessivel"] is False
        assert resultado["status"] == 403


# ---------------------------------------------------------------------------
# Contrato de dados (ponte com database)
# ---------------------------------------------------------------------------
class TestContrato:
    def _evento(self, **kw) -> EventoColetado:
        base = dict(
            fonte_id="tca",
            titulo="Concerto de Teste",
            inicio_em=datetime(2026, 12, 1, 20, 0, tzinfo=FUSO),
            url_original="https://www.ba.gov.br/tca/eventos/teste",
            metodo_extracao="json_embutido",
            confianca="alta",
            trecho="Concerto de Teste — 01/12/2026",
        )
        base.update(kw)
        return EventoColetado(**base)

    def test_hash_sha256_estavel(self):
        evento = self._evento()
        assert len(evento.hash_conteudo()) == 64
        assert evento.hash_conteudo() == self._evento().hash_conteudo()
        assert evento.hash_conteudo() != self._evento(titulo="Outro").hash_conteudo()

    def test_kwargs_compatíveis_com_evento_do_banco(self):
        Evento = pytest.importorskip("database.models").Evento
        kwargs = self._evento(local_nome="Sala Principal").evento_kwargs()
        evento = Evento(**kwargs)
        assert evento.titulo == "Concerto de Teste"
        assert evento.municipio == "Salvador" and evento.uf == "BA"

    def test_kwargs_compatíveis_com_proveniencia(self):
        Proveniencia = pytest.importorskip("database.models").Proveniencia
        evento = self._evento()
        prov = Proveniencia(
            **evento.proveniencia_kwargs(
                fonte_id=1, sessao_id=2, capturado_em=datetime.now(timezone.utc)
            )
        )
        assert prov.hash_conteudo == evento.hash_conteudo()
        assert prov.trecho

    def test_to_dict_serializavel(self):
        dados = self._evento().to_dict()
        json.dumps(dados)  # não pode levantar
        assert dados["inicio_em"].startswith("2026-12-01T20:00:00")
        assert dados["revisao_necessaria"] is False

    def test_rejeita_titulo_curto(self):
        with pytest.raises(ValueError, match="título"):
            self._evento(titulo="AB")

    def test_rejeita_data_sem_fuso(self):
        with pytest.raises(ValueError, match="tzinfo"):
            self._evento(inicio_em=datetime(2026, 12, 1, 20, 0))

    def test_rejeita_esquema_perigoso(self):
        with pytest.raises(ValueError, match="url_original"):
            self._evento(url_original="javascript:alert(1)")

    def test_rejeita_fim_antes_do_inicio(self):
        with pytest.raises(ValueError, match="fim_em"):
            self._evento(fim_em=datetime(2026, 11, 1, 20, 0, tzinfo=FUSO))

    def test_limpeza_remove_controle_e_trunca(self):
        texto = limpar_texto("  linha\n\ncom\x00controle\t" + "x" * 50, limite=30)
        assert "\x00" not in texto
        assert texto.endswith("…")
        assert len(texto) <= 30


# ---------------------------------------------------------------------------
# Utilidades de data/texto
# ---------------------------------------------------------------------------
class TestUtilidades:
    @pytest.mark.parametrize(
        "entrada,esperado",
        [("agosto", 8), ("AGOSTO", 8), ("fev", 2), ("Março", 3), ("inexistente", None)],
    )
    def test_mes_de(self, entrada, esperado):
        assert mes_de(entrada) == esperado

    def test_com_hora_variações(self):
        dia = datetime(2026, 12, 1).date()
        assert com_hora(dia, "19h").hour == 19
        assert com_hora(dia, "19h30").minute == 30
        assert com_hora(dia, "19:45").minute == 45
        assert com_hora(dia, None).hour == 0

    def test_ano_provavel_avanca_para_agenda_futura(self):
        referencia = datetime(2026, 11, 3, tzinfo=FUSO)
        assert ano_provavel(12, 20, referencia) == (2026, False)
        ano, inferido = ano_provavel(1, 10, referencia)
        assert (ano, inferido) == (2027, True)

    def test_url_absoluta_rejeita_esquema_perigoso(self):
        base = "https://www.ba.gov.br/tca/eventos"
        assert url_absoluta(base, "/tca/eventos/x") == "https://www.ba.gov.br/tca/eventos/x"
        assert url_absoluta(base, "javascript:alert(1)") == ""
        assert url_absoluta(base, "") == ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class TestCli:
    def test_list_imprime_catalogo_completo(self, capsys):
        assert cli.main(["list"]) == 0
        saida = capsys.readouterr().out
        assert all(f.id in saida for f in CATALOGO)
        assert "resumo:" in saida

    def test_collect_de_fonte_bloqueada_retorna_2(self, capsys):
        assert cli.main(["collect", "--fonte", "muncab"]) == 2
        assert "FonteBloqueada" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Dublês
# ---------------------------------------------------------------------------
class StubFetcher:
    """Substitui o transporte: devolve fixtures e registra chamadas."""

    def __init__(self) -> None:
        self.paginas: dict[str, str] = {}
        self.chamadas: list[str] = []

    def baixar(self, url: str, *, fonte_id: str = ""):
        from collectors.fetch import PaginaBaixada, RespostaInvalida

        self.chamadas.append(url)
        if url not in self.paginas:
            raise RespostaInvalida(f"sem fixture para {url}", fonte_id=fonte_id, url=url)
        return PaginaBaixada(
            url_pedida=url,
            url_final=url,
            status=200,
            content_type="text/html; charset=utf-8",
            texto=self.paginas[url],
            tamanho=len(self.paginas[url]),
            buscado_em=datetime.now(timezone.utc),
            duracao_s=0.0,
        )

    def sondar(self, url: str, *, fonte_id: str = ""):
        self.chamadas.append(url)
        return {"acessivel": url in self.paginas, "fonte_id": fonte_id}


@pytest.fixture()
def stub_fetcher() -> StubFetcher:
    return StubFetcher()
