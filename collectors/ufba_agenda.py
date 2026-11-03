"""Coletor da Agenda UFBA (portal institucional, ``ufba.br/agenda-ufba``).

O que a página publica (observado em 2026-11-03, HTTP 200, 59 KB, robots
``Allow: /``):

* a agenda é uma *view* Drupal renderizada no servidor, em tabela
  (``table.views-table``) com uma linha por evento;
* cada linha traz, em parágrafos com classe própria, o que a fonte afirma:
  ``p.agenda-categoria`` (Cursos, Eventos, Artes, Defesas…),
  ``p.agenda-titulo > a`` (título e URL do evento),
  ``p.agenda-data`` com ``span.date-display-single`` **ou**
  ``span.date-display-start``/``span.date-display-end`` no formato
  ``dd/mm/aaaa - HhMM`` e ``p.agenda-local``;
* os parágrafos restantes da célula são a descrição publicada.

Nada é inferido: título, categoria, local, início e fim vêm do HTML. A hora
publicada é a que o editor digitou (inclusive valores "de preenchimento" como
8h08) — o coletor não corrige nem arredonda. Só a URL é absolutizada.

``agenda.ufba.br`` (portal separado de eventos) **não** é usado aqui: o TLS
dele apresenta cadeia incompleta e o cliente do projeto não desativa
verificação de certificado (ver catálogo).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sources.catalog import FonteCultural

from . import htmldom
from .base import ColetorBase
from .fetch import HttpFetcher
from .models import EventoColetado, limpar_texto
from .textutil import FUSO_SALVADOR, url_absoluta

_DATA_HORA = re.compile(
    r"(?P<dia>\d{1,2})/(?P<mes>\d{1,2})/(?P<ano>\d{4})"
    r"\s*[-–]\s*(?P<hora>\d{1,2})h(?P<minuto>\d{2})?"
)
def data_hora_publicada(texto: str) -> datetime | None:
    """Converte ``'26/10/2026 - 8h08'`` em datetime em America/Bahia."""
    achado = _DATA_HORA.search(texto or "")
    if not achado:
        return None
    partes = {k: int(v) if v else 0 for k, v in achado.groupdict().items()}
    try:
        return datetime(
            partes["ano"],
            partes["mes"],
            partes["dia"],
            partes["hora"],
            partes["minuto"],
            tzinfo=FUSO_SALVADOR,
        )
    except ValueError:
        return None


def _texto_do_span(celula: htmldom.No, classes: tuple[str, ...]) -> str:
    for span in celula.find_all("span"):
        if span.tem_classe(*classes):
            texto = span.texto()
            if texto:
                return texto
    return ""


def _descricao(celula: htmldom.No) -> str:
    partes = []
    for paragrafo in celula.find_all("p"):
        if paragrafo.tem_classe("agenda-categoria", "agenda-titulo", "agenda-data", "agenda-local"):
            continue
        texto = paragrafo.texto()
        if texto:
            partes.append(texto)
    return limpar_texto(" ".join(partes), 1000)


def extrair_agenda(html: str, url_base: str) -> tuple[list[dict[str, object]], int]:
    """Extrai as linhas da agenda; devolve ``(itens, ignorados)``."""
    documento = htmldom.parse(html)
    itens: list[dict[str, object]] = []
    ignorados = 0
    for titulo_p in documento.find_all("p", "agenda-titulo"):
        link = titulo_p.find("a")
        if link is None:
            ignorados += 1
            continue
        celula = titulo_p.parent
        while celula is not None and celula.tag != "td":
            celula = celula.parent
        if celula is None:
            ignorados += 1
            continue
        titulo = limpar_texto(link.texto(), 300)
        url = url_absoluta(url_base, link.get("href"))
        if len(titulo) < 3 or not url:
            ignorados += 1
            continue

        data_texto = _texto_do_span(celula, ("date-display-single", "date-display-start"))
        if not data_texto:
            # Alguns temas mudam a classe; aceita o parágrafo de data publicado.
            data_p = celula.find("p", "agenda-data")
            data_texto = data_p.texto() if data_p is not None else ""
        inicio = data_hora_publicada(data_texto)
        if inicio is None:
            ignorados += 1
            continue

        fim_texto = _texto_do_span(celula, ("date-display-end",))
        fim = data_hora_publicada(fim_texto) if fim_texto else None
        if fim is not None and fim < inicio:
            fim = None

        categoria_p = celula.find("p", "agenda-categoria")
        local_p = celula.find("p", "agenda-local")
        local = limpar_texto(local_p.texto() if local_p is not None else "", 200)
        categoria = limpar_texto(categoria_p.texto() if categoria_p is not None else "", 80)

        itens.append(
            {
                "titulo": titulo,
                "url": url,
                "inicio": inicio,
                "fim": fim,
                "inicio_texto": limpar_texto(data_texto, 60),
                "fim_texto": limpar_texto(fim_texto, 60),
                "local": local,
                "categoria": categoria,
                "descricao": _descricao(celula),
            }
        )
    return itens, ignorados


class ColetorAgendaUFBA(ColetorBase):
    """Agenda institucional da UFBA (tabela Drupal renderizada no servidor)."""

    def __init__(
        self,
        fonte: FonteCultural,
        fetcher: HttpFetcher | None = None,
        *,
        filtrar_passados: bool = True,
        agora: datetime | None = None,
    ) -> None:
        super().__init__(fonte, fetcher)
        self.filtrar_passados = filtrar_passados
        self.agora = agora
        self.ultimo_estado: dict[str, object] = {}

    def _coletar(self) -> list[EventoColetado]:
        pagina = self.baixar()
        itens, ignorados = extrair_agenda(pagina.texto, pagina.url_final or self.fonte.url)
        referencia = self.agora or datetime.now(FUSO_SALVADOR)
        corte = referencia - timedelta(days=1)
        eventos: list[EventoColetado] = []
        for item in itens:
            fim = item["fim"] if isinstance(item["fim"], datetime) else None
            if self.filtrar_passados and (fim or item["inicio"]) < corte:
                continue
            inicio = item["inicio"]
            trecho = " — ".join(
                parte
                for parte in (
                    str(item["titulo"]),
                    str(item["inicio_texto"]),
                    str(item["local"]),
                )
                if parte
            )
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=str(item["titulo"]),
                        inicio_em=inicio,
                        fim_em=fim,
                        url_original=str(item["url"]),
                        metodo_extracao="atributo_html",
                        confianca="alta",
                        trecho=trecho,
                        descricao=str(item["descricao"]),
                        local_nome=str(item["local"]),
                        categoria=str(item["categoria"]).lower(),
                    )
                )
            except ValueError:
                ignorados += 1
        self.ultimo_estado = {
            "tabela_encontrada": bool(itens) or "views-table" in pagina.texto,
            "linhas": len(itens),
            "ignorados": ignorados,
            "eventos": len(eventos),
        }
        return eventos


__all__ = ["ColetorAgendaUFBA", "data_hora_publicada", "extrair_agenda"]
