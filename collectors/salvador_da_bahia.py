"""Coletor do portal Salvador da Bahia (agenda do dia).

Estrutura observada (WordPress + Yoast):

* cada dia é um ``div.box-header-agenda`` com ``h2`` do tipo
  ``Terça-feira 03 de Novembro de 2026``;
* abaixo, ``div.card`` com ``a.title`` (nome + link do evento) e
  ``div.resume`` (local e endereço).

O portal não publica hora no cartão. O evento entra à meia-noite em
America/Bahia e é marcado com ``hora`` em ``campos_inferidos``, o que força
revisão humana antes de publicação.
"""

from __future__ import annotations

import re

from sources.catalog import FonteCultural

from . import htmldom
from .base import ColetorBase
from .fetch import HttpFetcher
from .models import EventoColetado, limpar_texto
from .textutil import data_local, mes_de, url_absoluta

_DATA_CABECALHO = re.compile(r"(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{4})")


def _data_do_cabecalho(texto: str) -> tuple[int, int, int] | None:
    achado = _DATA_CABECALHO.search(texto or "")
    if not achado:
        return None
    dia, nome_mes, ano = achado.groups()
    mes = mes_de(nome_mes)
    if mes is None:
        return None
    return int(ano), mes, int(dia)


def extrair_cartoes(html: str, url_base: str) -> tuple[list[dict[str, str]], int]:
    """Extrai cartões de agenda; devolve ``(itens, descartados)``."""
    documento = htmldom.parse(html)
    itens: list[dict[str, str]] = []
    descartados = 0
    data_atual: tuple[int, int, int] | None = None

    for no in htmldom.em_ordem(documento):
        if no.tem_classe("box-header-agenda"):
            cabecalho = no.find("h2") or no
            data_atual = _data_do_cabecalho(cabecalho.texto())
            continue
        if no.tag != "div" or not no.tem_classe("card"):
            continue
        if data_atual is None:
            descartados += 1
            continue

        link = no.find("a", "title")
        if link is None:
            descartados += 1
            continue
        titulo = limpar_texto(link.texto() or link.get("title"), 300)
        href = url_absoluta(url_base, link.get("href"))
        if len(titulo) < 3 or not href:
            descartados += 1
            continue

        local = ""
        endereco = ""
        resumo = no.find("div", "resume")
        if resumo is not None:
            span = resumo.find("span")
            if span is not None:
                local = limpar_texto(span.texto(), 200)
            paragrafo = resumo.find("p")
            if paragrafo is not None:
                endereco = limpar_texto(paragrafo.texto(), 300).strip("() ")

        ano, mes, dia = data_atual
        itens.append(
            {
                "titulo": titulo,
                "url": href,
                "local": local,
                "endereco": endereco,
                "ano": str(ano),
                "mes": str(mes),
                "dia": str(dia),
                "trecho": limpar_texto(f"{titulo} — {local}", 200),
            }
        )
    return itens, descartados


class ColetorSalvadorDaBahia(ColetorBase):
    def __init__(self, fonte: FonteCultural, fetcher: HttpFetcher | None = None) -> None:
        super().__init__(fonte, fetcher)
        self.ultimo_estado: dict[str, object] = {}

    def _coletar(self) -> list[EventoColetado]:
        pagina = self.baixar()
        itens, descartados = extrair_cartoes(pagina.texto, self.fonte.url)
        eventos: list[EventoColetado] = []
        for item in itens:
            inicio = data_local(int(item["ano"]), int(item["mes"]), int(item["dia"]))
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=item["titulo"],
                        inicio_em=inicio,
                        url_original=item["url"],
                        metodo_extracao="cartao_html",
                        confianca="media",
                        trecho=item["trecho"],
                        local_nome=item["local"],
                        endereco=item["endereco"],
                        campos_inferidos=("hora",),
                    )
                )
            except ValueError:
                descartados += 1
        self.ultimo_estado = {
            "cartoes": len(itens),
            "descartados": descartados,
            "eventos": len(eventos),
        }
        return eventos


__all__ = ["ColetorSalvadorDaBahia", "extrair_cartoes"]
