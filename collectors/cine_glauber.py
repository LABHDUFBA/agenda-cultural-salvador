"""Coletor das sessões do Cine Glauber Rocha (cinema da UFBA, Salvador).

A página ``/glauber-rocha/programacao`` é HTML renderizado no servidor
(observado em 2026-11-03, HTTP 200, 299 KB, robots ``Allow`` com
``Disallow`` apenas para lightbox/admin). Cada filme é um ``div.filme``:

* ``h2.programacao-filme-title`` — título publicado;
* link ``/glauber-rocha/filme/<id>/<slug>`` — página do filme (proveniência);
* um ``div.sala-filme`` por sessão, com ``div.sala`` (sala), ``div.audio``
  (áudio/legenda), o link de compra e, em ``span.visually-hidden``, a frase
  acessível publicada pela própria página:
  ``ingresso para <FILME>, sessão de dd/mm/aaaa às HH:MM, sala N, NAC 2D``.

A data e a hora vêm dessa frase (texto publicado), não do parâmetro
``sessao=ddmmaaaa_hhmm`` da URL de ingresso — a URL é usada apenas como
``url_ingresso``. Nenhum campo é inferido; ``local_nome`` combina o nome do
cinema (fonte) com a sala publicada.

O ano é sempre publicado aqui, ao contrário do widget da Casa Rosa.
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

_SESSAO = re.compile(
    r"sess[ãa]o\s+de\s+(?P<dia>\d{1,2})/(?P<mes>\d{1,2})/(?P<ano>\d{4})"
    r"\s+[àa]s\s+(?P<hora>\d{1,2}):(?P<minuto>\d{2})",
    re.IGNORECASE,
)


def data_da_sessao(texto: str) -> datetime | None:
    """Interpreta a frase acessível da sessão em America/Bahia."""
    achado = _SESSAO.search(texto or "")
    if not achado:
        return None
    try:
        return datetime(
            int(achado.group("ano")),
            int(achado.group("mes")),
            int(achado.group("dia")),
            int(achado.group("hora")),
            int(achado.group("minuto")),
            tzinfo=FUSO_SALVADOR,
        )
    except ValueError:
        return None


def _filme_url(bloco: htmldom.No, url_base: str) -> str:
    for link in bloco.find_all("a"):
        href = link.get("href")
        if "/filme/" in href:
            return url_absoluta(url_base, href)
    return ""


def extrair_sessoes(html: str, url_base: str) -> tuple[list[dict[str, object]], int]:
    """Extrai uma linha por sessão; devolve ``(itens, ignorados)``."""
    documento = htmldom.parse(html)
    itens: list[dict[str, object]] = []
    ignorados = 0
    for bloco in documento.find_all("div", "filme"):
        titulo_no = bloco.find("h2", "programacao-filme-title") or bloco.find("h2")
        titulo = limpar_texto(titulo_no.texto() if titulo_no is not None else "", 300)
        if len(titulo) < 3:
            ignorados += len(bloco.find_all("div", "sala-filme")) or 1
            continue
        url_filme = _filme_url(bloco, url_base)
        for sessao in bloco.find_all("div", "sala-filme"):
            sala_no = sessao.find("div", "sala")
            audio_no = sessao.find("div", "audio")
            link = sessao.find("a")
            oculto = ""
            for span in sessao.find_all("span"):
                if span.tem_classe("visually-hidden"):
                    texto = span.texto()
                    if "sess" in texto.lower():
                        oculto = texto
                        break
            inicio = data_da_sessao(oculto)
            if inicio is None:
                ignorados += 1
                continue
            sala = limpar_texto(sala_no.texto() if sala_no is not None else "", 80)
            audio = limpar_texto(audio_no.texto() if audio_no is not None else "", 80)
            href = url_absoluta(url_base, link.get("href")) if link is not None else ""
            itens.append(
                {
                    "titulo": titulo,
                    "inicio": inicio,
                    "sala": sala,
                    "audio": audio,
                    "url_filme": url_filme or url_base,
                    "url_ingresso": href if href.startswith("https://") else "",
                    "trecho": oculto,
                }
            )
    return itens, ignorados


class ColetorCineGlauber(ColetorBase):
    """Sessões de cinema publicadas pelo Cine Glauber Rocha (UFBA)."""

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
        itens, ignorados = extrair_sessoes(pagina.texto, pagina.url_final or self.fonte.url)
        referencia = self.agora or datetime.now(FUSO_SALVADOR)
        corte = referencia - timedelta(days=1)
        eventos: list[EventoColetado] = []
        for item in itens:
            inicio = item["inicio"]
            if self.filtrar_passados and inicio < corte:
                continue
            sala = str(item["sala"])
            local = "Cine Glauber Rocha" + (f" — {sala}" if sala else "")
            descricao = " · ".join(
                parte for parte in (sala, str(item["audio"])) if parte
            )
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=str(item["titulo"]),
                        inicio_em=inicio,
                        url_original=str(item["url_filme"]),
                        url_ingresso=str(item["url_ingresso"]),
                        metodo_extracao="cartao_html",
                        confianca="alta",
                        trecho=str(item["trecho"]),
                        descricao=descricao,
                        local_nome=local,
                        categoria="cinema",
                    )
                )
            except ValueError:
                ignorados += 1
        self.ultimo_estado = {
            "filmes": len(pagina.texto.split("programacao-filme-title")) - 1,
            "sessoes": len(itens),
            "ignorados": ignorados,
            "eventos": len(eventos),
        }
        return eventos


__all__ = ["ColetorCineGlauber", "data_da_sessao", "extrair_sessoes"]
