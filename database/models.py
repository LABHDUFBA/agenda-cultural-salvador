"""Modelos de entrada e utilitários de data.

Regra de fuso: valores sem offset são interpretados em America/Bahia e
gravados sempre em UTC (ISO 8601 com +00:00). Assim as comparações
lexicográficas no SQLite continuam válidas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

FUSO_BAHIA = ZoneInfo("America/Bahia")


def para_dt(valor: datetime | date | str) -> datetime:
    """Converte datetime/date/str ISO em datetime UTC consciente.

    Aceita "2026-11-03", "2026-11-03T20:00:00" (assume Bahia) e
    "2026-11-03T23:00:00+00:00"/"Z". Levanta ValueError se não entender.
    """
    if isinstance(valor, datetime):
        dt = valor
    elif isinstance(valor, date):
        dt = datetime.combine(valor, time.min)
    elif isinstance(valor, str):
        texto = valor.strip()
        if not texto:
            raise ValueError("data vazia")
        try:
            dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.combine(date.fromisoformat(texto), time.min)
    else:
        raise ValueError(f"tipo de data não suportado: {type(valor)!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_BAHIA)
    return dt.astimezone(timezone.utc)


def para_iso(valor: datetime | date | str) -> str:
    """Serializa em UTC com offset explícito, precisão de segundos."""
    return para_dt(valor).isoformat(timespec="seconds")


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Fonte:
    nome: str
    url_base: str
    tipo: str = "site"
    confiabilidade: str = "media"


@dataclass(frozen=True, slots=True)
class Sessao:
    fonte_id: int
    iniciada_em: datetime | str | None = None
    status: str = "aberta"


@dataclass(frozen=True, slots=True)
class Evento:
    """Evento candidato. Status não é definido pelo coletor: entra pendente."""

    titulo: str
    inicio_em: datetime | date | str
    municipio: str = ""
    uf: str = ""
    local_nome: str = ""
    descricao: str = ""
    fim_em: datetime | date | str | None = None
    endereco: str = ""
    bairro: str = ""
    categoria: str = ""
    url_ingresso: str = ""
    preco: str = ""
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True, slots=True)
class Proveniencia:
    """De onde o evento veio. Obrigatória em toda ingestão."""

    fonte_id: int
    url_original: str
    sessao_id: int | None = None
    capturado_em: datetime | str | None = None
    hash_conteudo: str = ""
    trecho: str = field(default="")
