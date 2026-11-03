"""Coletor da agenda da Escola de Teatro da UFBA (``teatro.ufba.br/agenda/``).

A página é um app Next.js cujo HTML entregue contém o payload de build em
``<script id="__NEXT_DATA__" type="application/json">``. Em
``props.pageProps.agenda`` está a lista completa publicada pela unidade
(observado em 2026-11-03, HTTP 200):

* ``uid`` — identifica a página do evento (``/agenda/<uid>/``);
* ``title``, ``subtitulo``, ``local`` e ``tipo`` (lista: Espetáculos, Eventos,
  Formações, Defesas…);
* ``date`` — ISO 8601 com offset, ex. ``2026-03-31T18:56:00+0000``.

O instante publicado está em UTC; o coletor **converte** para America/Bahia
(conversão, não inferência). Nenhum campo é inventado: quando ``local`` ou
``tipo`` vêm vazios, o evento sai sem eles (e o núcleo registra a ausência).

Estado observado na sondagem: 63 registros, o mais recente em 31/03/2026 —
ou seja, a unidade não tinha evento futuro publicado naquele dia. Coleta
válida com resultado zero não é falha; é ausência de evento na fonte.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from sources.catalog import FonteCultural

from .base import ColetorBase
from .fetch import HttpFetcher
from .models import EventoColetado, limpar_texto
from .textutil import FUSO_SALVADOR, url_absoluta

_PAYLOAD = re.compile(
    r"<script[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(?P<json>.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)


def _instante(valor: object) -> datetime | None:
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        momento = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if momento.tzinfo is None:
        # Payload sem offset não é reinterpretado: sem fuso, descarta.
        return None
    return momento.astimezone(FUSO_SALVADOR)


def extrair_agenda(html: str, url_base: str) -> list[dict[str, object]]:
    """Lê ``pageProps.agenda`` do payload Next.js; lista vazia se ausente."""
    achado = _PAYLOAD.search(html or "")
    if not achado:
        return []
    try:
        dados = json.loads(achado.group("json"))
    except json.JSONDecodeError:
        return []
    if not isinstance(dados, dict):
        return []
    agenda = (
        dados.get("props", {}).get("pageProps", {}).get("agenda")
        if isinstance(dados.get("props"), dict)
        else None
    )
    if not isinstance(agenda, list):
        return []

    itens: list[dict[str, object]] = []
    for bruto in agenda:
        if not isinstance(bruto, dict):
            continue
        inicio = _instante(bruto.get("date"))
        titulo = limpar_texto(bruto.get("title"), 300)
        uid = limpar_texto(bruto.get("uid"), 200)
        if inicio is None or len(titulo) < 3 or not uid:
            continue
        tipos = bruto.get("tipo") or []
        if isinstance(tipos, str):
            tipos = [tipos]
        categorias = [limpar_texto(t, 60) for t in tipos if isinstance(t, str) and t.strip()]
        itens.append(
            {
                "uid": uid,
                "titulo": titulo,
                "inicio": inicio,
                "url": url_absoluta(url_base, f"/agenda/{uid}/") or url_base,
                "local": limpar_texto(bruto.get("local"), 200),
                "categoria": ", ".join(categorias),
                "subtitulo": limpar_texto(bruto.get("subtitulo"), 500),
            }
        )
    return itens


class ColetorTeatroUFBA(ColetorBase):
    """Agenda da Escola de Teatro da UFBA (payload de hidratação Next.js)."""

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
        itens = extrair_agenda(pagina.texto, pagina.url_final or self.fonte.url)
        referencia = self.agora or datetime.now(FUSO_SALVADOR)
        corte = referencia - timedelta(days=1)
        eventos: list[EventoColetado] = []
        for item in itens:
            inicio = item["inicio"]
            if self.filtrar_passados and inicio < corte:
                continue
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=str(item["titulo"]),
                        inicio_em=inicio,
                        url_original=str(item["url"]),
                        metodo_extracao="json_embutido",
                        confianca="alta",
                        trecho=" — ".join(
                            parte
                            for parte in (
                                str(item["titulo"]),
                                inicio.strftime("%d/%m/%Y %H:%M"),
                                str(item["local"]),
                            )
                            if parte
                        ),
                        descricao=str(item["subtitulo"]),
                        local_nome=str(item["local"]),
                        categoria=str(item["categoria"]).lower(),
                    )
                )
            except ValueError:
                continue
        self.ultimo_estado = {
            "agenda_encontrada": "__NEXT_DATA__" in pagina.texto,
            "registros": len(itens),
            "eventos": len(eventos),
        }
        return eventos


__all__ = ["ColetorTeatroUFBA", "extrair_agenda"]
