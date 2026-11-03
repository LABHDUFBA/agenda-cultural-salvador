"""Coletor do Sympla — listagem de eventos de Salvador (BA).

O que a página publica (observado em 2026-11-03, HTTP 200, robots permitido):

* a listagem é uma aplicação Next.js (App Router) e o HTML entregue já contém,
  nos scripts ``self.__next_f.push([1, "…"])``, o **payload de hidratação**
  com os eventos da consulta — título, URL canônica do evento, ``start_date``
  e ``end_date`` em ISO 8601 com offset, local, endereço, bairro, cidade, UF
  e coordenadas;
* o mesmo payload se repete em blocos diferentes (listas "explore mais"), daí
  a deduplicação por URL canônica antes de montar os eventos.

Isto é dado **publicado pela própria página** (método ``json_embutido``), não
API privada: nenhuma chamada adicional é feita, nenhum endpoint é adivinhado e
não há execução de JavaScript. Se o layout mudar e o payload desaparecer, o
coletor falha alto com :class:`FonteNaoSuportada` em vez de devolver zero
eventos em silêncio.

Datas vêm com offset (UTC, ``+00:00``); são convertidas para America/Bahia
apenas para exibição — a conversão é determinística, não é inferência. Eventos
cuja cidade publicada não seja Salvador/BA são descartados, **nunca**
reinterpretados como se fossem de Salvador.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Iterator

from sources.catalog import FonteCultural

from .base import ColetorBase
from .fetch import FonteNaoSuportada, HttpFetcher
from .models import EventoColetado, limpar_texto
from .textutil import FUSO_SALVADOR, normalizar

_CHUNK_FLIGHT = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)


def payload_flight(html: str) -> str:
    """Concatena os pedaços do payload RSC embutidos no HTML.

    Pedaço truncado (página cortada por limite de tamanho) é ignorado: o
    restante continua legível e o coletor relata o que conseguiu ler.
    """
    partes: list[str] = []
    for bruto in _CHUNK_FLIGHT.findall(html or ""):
        try:
            partes.append(json.loads(bruto))
        except ValueError:
            continue
    return "".join(partes)


def objetos_json(payload: str) -> Iterator[dict]:
    """Percorre **objetos de evento** completos do payload.

    Só devolve objetos que tenham ``url`` e ``start_date`` — o formato que o
    Sympla publica para um evento —, o que descarta objetos pais (listas
    envolvendo os eventos) sem depender da posição no payload.

    Pilha de chaves consciente de strings: ``{``/``}`` dentro de valores não
    confundem o recorte.
    """
    pilha: list[int] = []
    em_string = False
    escape = False
    for indice, caractere in enumerate(payload):
        if em_string:
            if escape:
                escape = False
            elif caractere == "\\":
                escape = True
            elif caractere == '"':
                em_string = False
            continue
        if caractere == '"':
            em_string = True
        elif caractere == "{":
            pilha.append(indice)
        elif caractere == "}" and pilha:
            inicio = pilha.pop()
            trecho = payload[inicio : indice + 1]
            if '"start_date"' not in trecho or '"url"' not in trecho:
                continue
            try:
                objeto = json.loads(trecho)
            except ValueError:
                continue
            if isinstance(objeto, dict) and {"url", "start_date"} <= objeto.keys():
                yield objeto


def _data(valor: object) -> datetime | None:
    if not isinstance(valor, str) or not valor.strip():
        return None
    texto = valor.strip().replace("Z", "+00:00")
    try:
        data = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if data.tzinfo is None:  # fonte sem offset: não inventamos fuso
        return None
    return data.astimezone(FUSO_SALVADOR)


def eventos_do_payload(payload: str) -> list[dict]:
    """Objetos de evento únicos (por URL canônica), preferindo o mais completo."""
    por_url: dict[str, dict] = {}
    ordem: list[str] = []
    for objeto in objetos_json(payload):
        url = str(objeto.get("url") or "")
        if not url.startswith("https://bileto.sympla.com.br/event/"):
            continue
        if url not in por_url:
            ordem.append(url)
            por_url[url] = objeto
            continue
        atual = por_url[url]
        local_atual = atual.get("location") or {}
        local_novo = objeto.get("location") or {}
        if not local_atual.get("name") and local_novo.get("name"):
            por_url[url] = objeto
    return [por_url[url] for url in ordem]


def extrair_eventos(html: str) -> tuple[list[dict], int]:
    """Devolve ``(itens, descartados)`` a partir do HTML da listagem."""
    payload = payload_flight(html)
    if not payload:
        raise FonteNaoSuportada(
            "Sympla: payload de hidratação ausente no HTML entregue",
            url="https://www.sympla.com.br/eventos/salvador-ba",
        )
    itens: list[dict] = []
    descartados = 0
    for objeto in eventos_do_payload(payload):
        nome = limpar_texto(str(objeto.get("name") or ""), 300)
        inicio = _data(objeto.get("start_date"))
        local = objeto.get("location") or {}
        if len(nome) < 3 or inicio is None or not isinstance(local, dict):
            descartados += 1
            continue
        if normalizar(str(local.get("city") or "")) != "salvador":
            descartados += 1
            continue
        if normalizar(str(local.get("state") or "")) != "ba":
            descartados += 1
            continue
        formato = objeto.get("start_date_formats") or {}
        legenda = ""
        if isinstance(formato, dict):
            legenda = limpar_texto(str(formato.get("pt") or ""), 80)
        itens.append(
            {
                "titulo": nome,
                "url": str(objeto.get("url")),
                "inicio": inicio,
                "fim": _data(objeto.get("end_date")),
                "legenda_data": legenda,
                "local": limpar_texto(str(local.get("name") or ""), 200),
                "endereco": limpar_texto(str(local.get("address") or ""), 300),
                "bairro": limpar_texto(str(local.get("neighborhood") or ""), 120),
                "municipio": limpar_texto(str(local.get("city") or ""), 120),
                "uf": limpar_texto(str(local.get("state") or ""), 2),
                "latitude": local.get("lat") if isinstance(local.get("lat"), (int, float)) else None,
                "longitude": local.get("lon") if isinstance(local.get("lon"), (int, float)) else None,
                "evento_id": str(objeto.get("id") or ""),
            }
        )
    return itens, descartados


class ColetorSympla(ColetorBase):
    """Listagem pública do Sympla para Salvador, a partir do payload embutido."""

    def __init__(self, fonte: FonteCultural, fetcher: HttpFetcher | None = None) -> None:
        super().__init__(fonte, fetcher)
        self.ultimo_estado: dict[str, object] = {}

    def _coletar(self) -> list[EventoColetado]:
        pagina = self.baixar()
        itens, descartados = extrair_eventos(pagina.texto)
        eventos: list[EventoColetado] = []
        for item in itens:
            fim = item["fim"]
            inicio = item["inicio"]
            if fim is not None and fim < inicio:
                fim = None
            trecho = " — ".join(
                parte
                for parte in (
                    item["titulo"],
                    item["legenda_data"],
                    f"{item['local']}, {item['municipio']}/{item['uf']}" if item["local"] else "",
                )
                if parte
            )
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=item["titulo"],
                        inicio_em=inicio,
                        fim_em=fim,
                        url_original=item["url"],
                        metodo_extracao="json_embutido",
                        confianca="alta",
                        trecho=trecho,
                        local_nome=item["local"],
                        endereco=item["endereco"],
                        bairro=item["bairro"],
                        municipio=item["municipio"] or "Salvador",
                        uf=item["uf"] or "BA",
                        url_ingresso=item["url"],
                        latitude=item["latitude"],
                        longitude=item["longitude"],
                    )
                )
            except ValueError:
                descartados += 1
        self.ultimo_estado = {
            "payload_encontrado": True,
            "objetos_unicos": len(itens) + descartados,
            "eventos_salvador": len(itens),
            "descartados": descartados,
            "eventos": len(eventos),
        }
        if not eventos and not itens:
            raise FonteNaoSuportada(
                "Sympla: payload presente, mas nenhum evento de Salvador/BA "
                f"({descartados} descartados) — layout pode ter mudado",
                fonte_id=self.fonte.id,
                url=self.fonte.url,
            )
        return eventos


__all__ = [
    "ColetorSympla",
    "eventos_do_payload",
    "extrair_eventos",
    "objetos_json",
    "payload_flight",
]
