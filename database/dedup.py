"""Normalização e deduplicação conservadora.

Duas camadas:

1. Exata: chave SHA-256 de (título, início, local, município) normalizados.
   Mesma chave significa mesmo evento, sem revisão humana.
2. Aproximada: mesmo dia local e títulos com similaridade >= 0.90. Nesse
   caso o evento NÃO é fundido: entra pendente com motivo
   "possivel_duplicado:<id>" para revisão.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher

from .models import FUSO_BAHIA, para_dt

SIMILARIDADE_LIMIAR = 0.90

_NAO_ALFANUM = re.compile(r"[^a-z0-9]+")


def normalizar_texto(valor: str | None) -> str:
    """Minúsculas, sem acentos, sem pontuação, espaços colapsados."""
    if not valor:
        return ""
    decomposto = unicodedata.normalize("NFKD", valor)
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return _NAO_ALFANUM.sub(" ", sem_acento.casefold()).strip()


def data_ref_local(inicio_em: datetime | str) -> str:
    """Data (YYYY-MM-DD) no fuso de Salvador, que é o que o público vê."""
    return para_dt(inicio_em).astimezone(FUSO_BAHIA).date().isoformat()


def chave_deduplicacao(
    *,
    titulo: str,
    inicio_em: datetime | str,
    local_nome: str = "",
    municipio: str = "Salvador",
) -> str:
    partes = (
        normalizar_texto(titulo),
        para_dt(inicio_em).isoformat(timespec="minutes"),
        normalizar_texto(local_nome),
        normalizar_texto(municipio),
    )
    return hashlib.sha256("|".join(partes).encode("utf-8")).hexdigest()


def similaridade(titulo_a: str, titulo_b: str) -> float:
    a, b = normalizar_texto(titulo_a), normalizar_texto(titulo_b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def buscar_similar(
    conn,
    *,
    titulo: str,
    data_ref: str,
    excluir_id: int | None = None,
    limiar: float = SIMILARIDADE_LIMIAR,
) -> int | None:
    """Devolve o id do evento pendente/aprovado mais parecido no mesmo dia.

    Só considera candidatos do mesmo dia local. Retorna None se nada
    alcançar o limiar.
    """
    sql = (
        "SELECT id, titulo_normalizado FROM eventos "
        "WHERE data_ref = ? AND status IN ('pendente', 'aprovado')"
    )
    params: list[object] = [data_ref]
    if excluir_id is not None:
        sql += " AND id <> ?"
        params.append(excluir_id)
    melhor_id, melhor = None, 0.0
    for linha in conn.execute(sql, params):
        score = similaridade(titulo, linha["titulo_normalizado"])
        if score > melhor:
            melhor, melhor_id = score, linha["id"]
    return melhor_id if melhor >= limiar else None
