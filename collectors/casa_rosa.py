"""Coletor do Casa Rosa (Rio Vermelho, Salvador) via widget público do Sympla.

O que a página publica (observado em 2026-11-03, HTTP 200, robots ausente):

* ``site.bileto.sympla.com.br/casarosa/`` é institucional: **não** traz evento
  no HTML, apenas um ``<iframe>`` público do widget de agenda do Sympla
  (``https://www.sympla.com.br/agenda-eventos/<token>?theme=light``);
* esse widget, servido pelo próprio Sympla, é HTML renderizado no servidor
  com os próximos eventos da casa: ``li.search-result-li`` →
  ``a.sympla-card`` com ``data-name`` (nome **completo**; o texto visível é
  truncado por CSS), ``.event-date-day``, ``.event-card-date-month`` (mês
  abreviado), ``.event-card-info`` (hora) e ``.event-location``.

São duas requisições, ambas a URLs que a própria página publica. Nada de API
privada, nada de JavaScript, nada de adivinhação de endpoint.

O ano **não** é publicado pelo widget: é o único campo inferido
(``ano_provavel``) e vai em ``campos_inferidos``, o que força revisão humana.
A hora é publicada e não é inferida.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from sources.catalog import FonteCultural

from . import htmldom
from .base import ColetorBase
from .fetch import FonteNaoSuportada, HttpFetcher
from .models import EventoColetado, limpar_texto
from .textutil import FUSO_SALVADOR, ano_provavel, com_hora, mes_de

_MARCA_WIDGET = "agenda-eventos"


def encontrar_widget(html: str, url_base: str) -> str:
    """URL absoluta do iframe/script de agenda do Sympla embutido na página."""
    documento = htmldom.parse(html)
    candidatos = []
    for no in documento.elementos():
        if no.tag not in ("iframe", "script", "link"):
            continue
        alvo = no.get("src") or no.get("data-src")
        if _MARCA_WIDGET in alvo and "sympla.com.br" in alvo:
            candidatos.append(urljoin(url_base, alvo))
    if not candidatos:
        return ""
    partes = urlsplit(candidatos[0])
    if partes.scheme not in ("http", "https"):
        return ""
    return candidatos[0]


def extrair_cartoes(html: str, url_base: str) -> tuple[list[dict[str, str]], int]:
    """Extrai os cartões do widget; devolve ``(itens, descartados)``."""
    documento = htmldom.parse(html)
    itens: list[dict[str, str]] = []
    descartados = 0
    for lista in documento.find_all("li", "search-result-li"):
        link = lista.find("a")
        if link is None:
            descartados += 1
            continue
        href = urljoin(url_base, link.get("href"))
        nome = limpar_texto(link.get("data-name") or "", 300)
        if len(nome) < 3 or not href.startswith(("http://", "https://")):
            descartados += 1
            continue

        dia = lista.find(class_="event-date-day")
        mes = lista.find(class_="event-card-date-month")
        hora = lista.find(class_="event-card-info")
        local = lista.find(class_="event-location")
        dia_txt = (dia.texto() if dia is not None else "").strip()
        mes_txt = (mes.texto() if mes is not None else "").strip()
        if not dia_txt.isdigit() or mes_de(mes_txt) is None:
            descartados += 1
            continue
        itens.append(
            {
                "titulo": nome,
                "url": href,
                "dia": dia_txt,
                "mes": mes_txt,
                "hora": (hora.texto() if hora is not None else "").strip(),
                "local": limpar_texto(local.texto() if local is not None else "", 200),
            }
        )
    return itens, descartados


def _local_sem_uf(texto: str) -> str:
    """'Casa Rosa - BA' → 'Casa Rosa' (o widget publica a UF junto do nome)."""
    return limpar_texto(re.sub(r"\s*-\s*[A-Z]{2}\s*$", "", texto or ""), 200)


class ColetorCasaRosa(ColetorBase):
    def __init__(self, fonte: FonteCultural, fetcher: HttpFetcher | None = None) -> None:
        super().__init__(fonte, fetcher)
        self.ultimo_estado: dict[str, object] = {}

    def _coletar(self) -> list[EventoColetado]:
        pagina = self.baixar()
        url_widget = encontrar_widget(pagina.texto, pagina.url_final or self.fonte.url)
        if not url_widget:
            raise FonteNaoSuportada(
                "Casa Rosa: página sem widget público de agenda no HTML — "
                "nada a extrair sem inventar eventos",
                fonte_id=self.fonte.id,
                url=self.fonte.url,
            )
        widget = self.fetcher.baixar(url_widget, fonte_id=self.fonte.id)
        itens, descartados = extrair_cartoes(widget.texto, widget.url_final)

        agora = datetime.now(FUSO_SALVADOR)
        eventos: list[EventoColetado] = []
        for item in itens:
            mes = mes_de(item["mes"])
            if mes is None:
                descartados += 1
                continue
            ano, ano_inferido = ano_provavel(mes, int(item["dia"]), agora)
            inicio = com_hora(
                datetime(ano, mes, int(item["dia"])).date(), item["hora"] or None
            )
            local = _local_sem_uf(item["local"]) or "Casa Rosa"
            trecho = " — ".join(
                parte
                for parte in (
                    item["titulo"],
                    f"{int(item['dia']):02d} {item['mes']} {ano}",
                    item["hora"],
                    local,
                )
                if parte
            )
            inferidos = ("ano",)  # o widget nunca publica o ano
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=item["titulo"],
                        inicio_em=inicio,
                        url_original=item["url"],
                        url_ingresso=item["url"],
                        metodo_extracao="cartao_html",
                        confianca="media",
                        trecho=trecho,
                        local_nome=local,
                        campos_inferidos=inferidos,
                    )
                )
            except ValueError:
                descartados += 1
        self.ultimo_estado = {
            "widget": url_widget,
            "cartoes": len(itens),
            "descartados": descartados,
            "eventos": len(eventos),
            "anos_inferidos": len(eventos),
        }
        return eventos


__all__ = ["ColetorCasaRosa", "encontrar_widget", "extrair_cartoes"]
