"""Coletor do Teatro Vila Velha (agenda mensal em texto corrido).

A agenda é publicada como um acordeão WordPress/Elementor:

* ``h1 > span.qodef-e-title`` = ``agosto/26`` (mês/ano da seção);
* ``div.qodef-e-content`` = parágrafos no formato
  ``TÍTULO - 12 e 13 de setembro - 16h - LOCAL (observação)``.

É a fonte mais frágil do catálogo: texto livre, ano no cabeçalho e sem hora
de término. Por isso todo evento sai com ``confianca="baixa"`` e
``campos_inferidos`` preenchido quando algo é deduzido.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sources.catalog import FonteCultural

from . import htmldom
from .base import ColetorBase
from .fetch import HttpFetcher
from .models import EventoColetado, limpar_texto
from .textutil import FUSO_SALVADOR, com_hora, mes_de

_SECAO = re.compile(r"^([A-Za-zÀ-ÿ]+)\s*/\s*(\d{2,4})$")
_EVENTO = re.compile(
    r"^(?P<titulo>.+?)\s+-\s+"
    r"(?P<dias>\d{1,2}(?:\s+e\s+\d{1,2})?(?:\s+a\s+\d{1,2})?)\s+de\s+"
    r"(?P<mes>[A-Za-zÀ-ÿ]+)"
    r"(?:\s+de\s+(?P<ano>\d{4}))?\s+-\s+"
    r"(?P<hora>\d{1,2}\s*h(?:\s*\d{2})?)\s+-\s+"
    r"(?P<local>.+)$",
    re.IGNORECASE,
)


def _ano_da_secao(texto: str) -> int | None:
    achado = _SECAO.match(texto.strip())
    if not achado:
        return None
    nome_mes, sufixo = achado.groups()
    if mes_de(nome_mes) is None:
        return None
    numero = int(sufixo)
    return 2000 + numero if numero < 100 else numero


def _dias(texto: str) -> list[int]:
    """'12 e 13' -> [12, 13]; '02 a 04' -> [2, 3, 4]."""
    simples = [int(n) for n in re.findall(r"\d{1,2}", texto)]
    if " a " in f" {texto} " and len(simples) == 2:
        inicio, fim = simples
        if 0 < inicio <= fim <= 31 and fim - inicio < 15:
            return list(range(inicio, fim + 1))
    return [dia for dia in simples if 1 <= dia <= 31]


def extrair_agenda(html: str, *, referencia: datetime | None = None) -> tuple[list[dict[str, object]], int]:
    """Extrai os itens de agenda do acordeão; devolve ``(itens, ignorados)``."""
    referencia = referencia or datetime.now(timezone.utc)
    documento = htmldom.parse(html)
    itens: list[dict[str, object]] = []
    ignorados = 0
    ano_secao: int | None = None
    mes_secao: int | None = None

    for no in htmldom.em_ordem(documento):
        if no.tem_classe("qodef-e-title"):
            texto = no.texto()
            ano = _ano_da_secao(texto)
            if ano is not None:
                ano_secao = ano
                mes_secao = mes_de(_SECAO.match(texto.strip()).group(1))  # type: ignore[union-attr]
            continue
        if no.tag != "p":
            continue
        conteudo = no.texto()
        if not conteudo or len(conteudo) < 8:
            continue
        achado = _EVENTO.match(conteudo)
        if not achado:
            continue
        grupos = achado.groupdict()
        mes = mes_de(grupos["mes"] or "") or mes_secao
        if mes is None:
            ignorados += 1
            continue
        ano = int(grupos["ano"]) if grupos.get("ano") else (ano_secao or referencia.year)
        titulo = limpar_texto(grupos["titulo"], 300)
        local_bruto = limpar_texto(grupos["local"], 300)
        local = re.sub(r"\s*\(.*\)\s*$", "", local_bruto).strip()
        observacao = ""
        parentese = re.search(r"\(([^)]*)\)\s*$", local_bruto)
        if parentese:
            observacao = limpar_texto(parentese.group(1), 200)
        dias = _dias(grupos["dias"] or "")
        if not dias or len(titulo) < 3:
            ignorados += 1
            continue
        for dia in dias:
            itens.append(
                {
                    "titulo": titulo,
                    "dia": dia,
                    "mes": mes,
                    "ano": ano,
                    "hora": grupos["hora"],
                    "local": local,
                    "observacao": observacao,
                    "trecho": limpar_texto(conteudo, 300),
                }
            )
    return itens, ignorados


class ColetorVilaVelha(ColetorBase):
    def __init__(self, fonte: FonteCultural, fetcher: HttpFetcher | None = None) -> None:
        super().__init__(fonte, fetcher)
        self.ultimo_estado: dict[str, object] = {}

    def _coletar(self) -> list[EventoColetado]:
        pagina = self.baixar()
        referencia = datetime.now(FUSO_SALVADOR)
        itens, ignorados = extrair_agenda(pagina.texto, referencia=referencia)
        eventos: list[EventoColetado] = []
        for item in itens:
            dia = int(item["dia"])
            mes = int(item["mes"])
            ano = int(item["ano"])
            inicio = com_hora(
                datetime(ano, mes, dia, tzinfo=FUSO_SALVADOR).date(),
                str(item["hora"]),
            )
            try:
                eventos.append(
                    EventoColetado(
                        fonte_id=self.fonte.id,
                        titulo=str(item["titulo"]),
                        inicio_em=inicio,
                        url_original=self.fonte.url,
                        metodo_extracao="texto_agenda",
                        confianca="baixa",
                        trecho=str(item["trecho"]),
                        descricao=str(item["observacao"]),
                        local_nome=str(item["local"]),
                        campos_inferidos=("ano", "hora_fim", "local_em_texto_livre"),
                    )
                )
            except ValueError:
                ignorados += 1
        self.ultimo_estado = {
            "itens": len(itens),
            "ignorados": ignorados,
            "eventos": len(eventos),
        }
        return eventos


__all__ = ["ColetorVilaVelha", "extrair_agenda"]
