"""Parsing de data/hora em português e utilidades de texto."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

FUSO_SALVADOR = ZoneInfo("America/Bahia")

MESES = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5, "mai": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}

_HORA = re.compile(r"(?P<h>\d{1,2})\s*h(?:\s*(?P<m>\d{2}))?|\b(?P<h2>\d{1,2})[:h](?P<m2>\d{2})\b")


def sem_acento(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def normalizar(texto: str) -> str:
    """Minúsculo, sem acento e com espaços colapsados — para comparação."""
    return " ".join(sem_acento(texto or "").lower().split())


def mes_de(nome: str) -> int | None:
    return MESES.get(normalizar(nome).strip(" ."))


def data_local(ano: int, mes: int, dia: int) -> datetime:
    """Data à meia-noite em America/Bahia (valor conservador para 'só dia')."""
    return datetime.combine(date(ano, mes, dia), time.min, tzinfo=FUSO_SALVADOR)


def com_hora(dia: date, hora_txt: str | None) -> datetime:
    """Aplica hora ('19h', '19h30', '19:30') ou meia-noite quando ausente."""
    if not hora_txt:
        return datetime.combine(dia, time.min, tzinfo=FUSO_SALVADOR)
    achado = _HORA.search(hora_txt)
    if not achado:
        return datetime.combine(dia, time.min, tzinfo=FUSO_SALVADOR)
    hora = int(achado.group("h") or achado.group("h2"))
    minuto = int(achado.group("m") or achado.group("m2") or 0)
    if hora > 23 or minuto > 59:
        return datetime.combine(dia, time.min, tzinfo=FUSO_SALVADOR)
    return datetime.combine(dia, time(hora, minuto), tzinfo=FUSO_SALVADOR)


def ano_provavel(mes: int, dia: int, referencia: datetime) -> tuple[int, bool]:
    """Ano para agendas que só publicam dia/mês.

    Devolve ``(ano, inferido)``. Regra: começa no ano de referência; se a data
    já passou há mais de 60 dias, assume o ano seguinte (agenda é futura).
    """
    candidato = date(referencia.year, mes, dia)
    inferido = False
    if candidato < (referencia.date() - timedelta(days=60)):
        candidato = date(referencia.year + 1, mes, dia)
        inferido = True
    return candidato.year, inferido


def url_absoluta(base: str, href: str) -> str:
    """Resolve link relativo e rejeita esquemas perigosos (javascript:, data:)."""
    from urllib.parse import urljoin, urlsplit

    if not href:
        return ""
    alvo = urljoin(base, href.strip())
    partes = urlsplit(alvo)
    if partes.scheme not in ("http", "https"):
        return ""
    return alvo


__all__ = [
    "FUSO_SALVADOR",
    "MESES",
    "ano_provavel",
    "com_hora",
    "data_local",
    "mes_de",
    "normalizar",
    "sem_acento",
    "url_absoluta",
]
