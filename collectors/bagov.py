"""Coletor do calendário Drupal usado pelos sites estaduais (ba.gov.br).

Os sites ``/tca``, ``/fundacaocultural``, ``/cultura`` e ``/ipac`` rodam o
mesmo tema Drupal e publicam o FullCalendar com os eventos embutidos em
``drupalSettings``. A extração é determinística e não executa JavaScript:

    1. localizar ``eventTimeFormat`` (marca do bloco do calendário);
    2. ler o array ``"events": [...]`` que vem logo depois;
    3. desescapar ``\\u0022`` / ``\\/`` e interpretar como JSON.

O calendário costuma trazer também eventos passados; o coletor não decide o
que é passado (isso é do núcleo), mas ``filtrar_passados`` permite ao
chamador reduzir ruído sem alterar a semântica.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sources.catalog import FonteCultural

from .base import ColetorBase
from .fetch import HttpFetcher
from .models import EventoColetado, limpar_texto
from .textutil import FUSO_SALVADOR, url_absoluta

MARCA_CALENDARIO = "eventTimeFormat"


def extrair_calendario(html: str, url_base: str) -> list[dict[str, object]]:
    """Devolve a lista de eventos do ``drupalSettings`` (vazia se não houver)."""
    texto = (html or "").replace("\\u0022", '"')
    marca = texto.find(MARCA_CALENDARIO)
    if marca < 0:
        return []
    inicio = texto.find('"events":[', marca)
    if inicio < 0:
        return []
    trecho = texto[inicio + len('"events":') :]

    profundidade = 0
    fim = -1
    for indice, caractere in enumerate(trecho):
        if caractere == "[":
            profundidade += 1
        elif caractere == "]":
            profundidade -= 1
            if profundidade == 0:
                fim = indice + 1
                break
    if fim < 0:
        return []

    bruto = (
        trecho[:fim]
        .replace("\\\\/", "/")
        .replace("\\/", "/")
        .replace("\\\\u", "\\u")
    )
    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError:
        return []
    if not isinstance(dados, list):
        return []
    return [item for item in dados if isinstance(item, dict) and item.get("title")]


def _data(valor: object) -> datetime | None:
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        momento = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=FUSO_SALVADOR)
    return momento


class ColetorBaGovCalendario(ColetorBase):
    """Calendário embutido dos sites estaduais de cultura."""

    def __init__(
        self,
        fonte: FonteCultural,
        fetcher: HttpFetcher | None = None,
        *,
        paginas: int = 1,
        filtrar_passados: bool = True,
    ) -> None:
        super().__init__(fonte, fetcher)
        self.paginas = max(1, paginas)
        self.filtrar_passados = filtrar_passados
        self.ultimo_estado: dict[str, object] = {}

    def _url_pagina(self, indice: int) -> str:
        if indice <= 1:
            return self.fonte.url
        separador = "&" if "?" in self.fonte.url else "?"
        return f"{self.fonte.url}{separador}page={indice}"

    def _coletar(self) -> list[EventoColetado]:
        eventos: list[EventoColetado] = []
        encontrados_brutos: list[dict[str, object]] = []
        for indice in range(1, self.paginas + 1):
            url = self._url_pagina(indice)
            pagina = self.fetcher.baixar(url, fonte_id=self.fonte.id)
            itens = extrair_calendario(pagina.texto, url)
            if not itens:
                if indice == 1:
                    self.ultimo_estado = {
                        "calendario_encontrado": MARCA_CALENDARIO in pagina.texto,
                        "eventos": 0,
                    }
                break
            encontrados_brutos.extend(itens)
            if len(itens) < 20:
                break

        corte = datetime.now(timezone.utc) - timedelta(days=1)
        for item in encontrados_brutos:
            inicio = _data(item.get("start"))
            if inicio is None:
                continue
            if self.filtrar_passados and inicio < corte:
                continue
            fim = _data(item.get("end"))
            titulo = limpar_texto(str(item.get("title", "")), 300)
            href = str(item.get("url", ""))
            url_evento = url_absoluta(self.fonte.url, href) or self.fonte.url
            descricao = limpar_texto(str(item.get("des", "")), 500)
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=titulo,
                        inicio_em=inicio,
                        fim_em=fim,
                        url_original=url_evento,
                        metodo_extracao="json_embutido",
                        confianca="alta",
                        trecho=descricao or titulo,
                        descricao=descricao,
                        local_nome="",  # o local só consta na página de detalhe
                        categoria="",
                    )
                )
            except ValueError:
                continue
        self.ultimo_estado = {
            "calendario_encontrado": True,
            "eventos_brutos": len(encontrados_brutos),
            "eventos_validos": len(eventos),
        }
        return eventos


__all__ = ["ColetorBaGovCalendario", "extrair_calendario"]
