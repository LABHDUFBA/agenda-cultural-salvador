"""Coletor da Bilheteria Digital — busca por Salvador (BA).

O que a página publica (observado em 2026-11-03, HTTP 200, robots permitido):

* ``/busca/salvador`` é HTML renderizado no servidor com até ~9 cartões
  ``li.box-li-evento``: link relativo do evento, ``.titulo-evento-thumb``,
  ``.data-evento-thumb`` ("15 de Novembro" — **sem ano e sem hora**),
  ``.cidade-box-evento`` ("Salvador - BA") e ``.local-box-evento``;
* a página de detalhe do evento (link publicado no próprio cartão) traz
  campos ocultos de formulário renderizados no servidor com o dado exato:
  ``nomeEvento``, ``local``, ``dataInicio`` em ISO 8601 com ``Z`` (UTC) e o
  preço exibido ("A PARTIR DE R$ 50,00").

Estratégia, na ordem:

1. ler a busca (1 requisição);
2. manter **apenas** cartões cuja cidade publicada seja Salvador/BA — evento
   de outra cidade é descartado, nunca reinterpretado;
3. para cada cartão, ler a página de detalhe (1 requisição, com o intervalo
   mínimo do ``HttpFetcher``) e usar ``dataInicio`` convertido de UTC para
   America/Bahia — conversão determinística, sem inferência;
4. se o detalhe falhar por motivo que não seja bloqueio, cair para a data do
   cartão e marcar ``ano`` e ``hora`` como inferidos, com confiança ``baixa``.

Bloqueio (401/403/429) em qualquer requisição **não** é contornado: a exceção
``FonteBloqueada`` sobe e encerra a coleta.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from sources.catalog import FonteCultural

from . import htmldom
from .base import ColetorBase
from .fetch import ErroColeta, FonteBloqueada, HttpFetcher
from .models import EventoColetado, limpar_texto
from .textutil import FUSO_SALVADOR, ano_provavel, com_hora, mes_de, normalizar

_DATA_CARTAO = re.compile(r"(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)")
_PRECO = re.compile(r"A PARTIR DE R\$\s*([\d.]+,\d{2})", re.I)
_MAX_DETALHES = 25


def extrair_cartoes(html: str, url_base: str) -> tuple[list[dict[str, str]], int]:
    """Cartões da busca; devolve ``(itens, descartados)``.

    Cartões de outras cidades entram em ``descartados`` — não são convertidos
    em evento de Salvador.
    """
    documento = htmldom.parse(html)
    itens: list[dict[str, str]] = []
    descartados = 0
    for lista in documento.find_all("li", "box-li-evento"):
        link = lista.find("a")
        titulo_no = lista.find(class_="titulo-evento-thumb")
        data_no = lista.find(class_="data-evento-thumb")
        cidade_no = lista.find(class_="cidade-box-evento")
        local_no = lista.find(class_="local-box-evento")
        titulo = limpar_texto(titulo_no.texto() if titulo_no is not None else "", 300)
        href = urljoin(url_base, link.get("href")) if link is not None else ""
        if "{{" in titulo or len(titulo) < 3 or not href.startswith(("http://", "https://")):
            descartados += 1
            continue
        cidade = limpar_texto(cidade_no.texto() if cidade_no is not None else "", 120)
        if normalizar(cidade) != "salvador - ba":
            descartados += 1
            continue
        itens.append(
            {
                "titulo": titulo,
                "url": href,
                "data": limpar_texto(data_no.texto() if data_no is not None else "", 80),
                "cidade": cidade,
                "local": limpar_texto(local_no.texto() if local_no is not None else "", 200),
            }
        )
    return itens, descartados


def extrair_detalhe(html: str) -> dict[str, object]:
    """Campos ocultos e preço publicados na página de detalhe do evento."""
    documento = htmldom.parse(html)
    ocultos: dict[str, str] = {}
    for campo in documento.find_all("input"):
        nome = campo.get("name") or campo.get("id")
        if nome:
            ocultos[nome] = campo.get("value")
    inicio = None
    bruto = ocultos.get("dataInicio", "")
    if bruto:
        try:
            data = datetime.fromisoformat(bruto.strip().replace("Z", "+00:00"))
        except ValueError:
            data = None
        if data is not None and data.tzinfo is not None:
            inicio = data.astimezone(FUSO_SALVADOR)
    texto = documento.texto()
    preco = _PRECO.search(texto)
    return {
        "titulo": limpar_texto(ocultos.get("nomeEvento", ""), 300),
        "local": limpar_texto(ocultos.get("local", ""), 200),
        "inicio": inicio,
        "data_bruta": bruto,
        "preco": f"R$ {preco.group(1)}" if preco else "",
        "evento_id": ocultos.get("eventoId", ""),
    }


def _data_do_cartao(texto: str, agora: datetime) -> tuple[datetime, tuple[str, ...]]:
    """Data do cartão ('15 de Novembro' ou '04 de Fevereiro a 09 de Fevereiro').

    Sem ano e sem hora na fonte: ambos são inferidos e declarados.
    """
    achado = _DATA_CARTAO.search(texto or "")
    if not achado:
        raise ValueError(f"data ilegível no cartão: {texto!r}")
    dia, nome_mes = achado.groups()
    mes = mes_de(nome_mes)
    if mes is None:
        raise ValueError(f"mês desconhecido no cartão: {nome_mes!r}")
    ano, _ = ano_provavel(mes, int(dia), agora)
    return com_hora(datetime(ano, mes, int(dia)).date(), None), ("ano", "hora")


class ColetorBilheteriaDigital(ColetorBase):
    def __init__(
        self,
        fonte: FonteCultural,
        fetcher: HttpFetcher | None = None,
        *,
        max_detalhes: int = _MAX_DETALHES,
    ) -> None:
        super().__init__(fonte, fetcher)
        self.max_detalhes = max_detalhes
        self.ultimo_estado: dict[str, object] = {}

    def _coletar(self) -> list[EventoColetado]:
        pagina = self.baixar()
        itens, descartados = extrair_cartoes(pagina.texto, pagina.url_final or self.fonte.url)
        agora = datetime.now(FUSO_SALVADOR)
        eventos: list[EventoColetado] = []
        detalhes = 0
        falhas_detalhe = 0

        for item in itens:
            if detalhes >= self.max_detalhes:
                break
            detalhes += 1
            inicio: datetime | None = None
            local = item["local"]
            preco = ""
            inferidos: tuple[str, ...] = ()
            metodo = "cartao_html"
            confianca = "baixa"
            try:
                detalhe = self.fetcher.baixar(item["url"], fonte_id=self.fonte.id)
            except FonteBloqueada:
                raise
            except ErroColeta:
                falhas_detalhe += 1
            else:
                dados = extrair_detalhe(detalhe.texto)
                inicio = dados["inicio"]  # type: ignore[assignment]
                local = str(dados["local"] or local)
                preco = str(dados["preco"] or "")
                if inicio is not None:
                    metodo = "atributo_html"
                    confianca = "alta"

            if inicio is None:
                try:
                    inicio, inferidos = _data_do_cartao(item["data"], agora)
                except ValueError:
                    descartados += 1
                    continue

            trecho = " — ".join(
                parte
                for parte in (
                    item["titulo"],
                    item["data"],
                    item["local"],
                    item["cidade"],
                )
                if parte
            )
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=item["titulo"],
                        inicio_em=inicio,
                        url_original=item["url"],
                        url_ingresso=item["url"],
                        metodo_extracao=metodo,
                        confianca=confianca,
                        trecho=trecho,
                        local_nome=local,
                        preco=preco,
                        campos_inferidos=inferidos,
                    )
                )
            except ValueError:
                descartados += 1

        self.ultimo_estado = {
            "cartoes_salvador": len(itens),
            "descartados": descartados,
            "detalhes_consultados": detalhes,
            "falhas_detalhe": falhas_detalhe,
            "eventos": len(eventos),
        }
        return eventos


__all__ = ["ColetorBilheteriaDigital", "extrair_cartoes", "extrair_detalhe"]
