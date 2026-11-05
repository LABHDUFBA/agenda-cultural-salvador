"""Domain model for the Agenda Cultural Salvador bot.

Pure data + validation.  No I/O, no network, no Telegram imports: this module
must stay importable and testable in complete isolation.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

MAX_TITLE = 160
MAX_VENUE = 160
MAX_DESCRIPTION = 1500
MAX_URL = 500
MAX_BAIRRO = 120
MAX_CATEGORIA = 80
MAX_PRECO = 120
MAX_ENDERECO = 300


class EventStatus(str, Enum):
    DRAFT = "draft"
    PENDING = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    DUPLICATE = "duplicado"          # status canônico vindo da deduplicação

    @property
    def is_public(self) -> bool:
        return self in (EventStatus.APPROVED, EventStatus.PUBLISHED)


class UserRole(str, Enum):
    PUBLIC = "public"
    EDITOR = "editor"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return {"public": 0, "editor": 1, "admin": 2}[self.value]


class DomainError(Exception):
    """Base class for errors that are safe to surface to a chat user."""


class ValidationError(DomainError):
    pass


class DuplicateEvent(DomainError):
    pass


class NotFound(DomainError):
    pass


class PermissionDenied(DomainError):
    pass


class PublishBlocked(DomainError):
    def __init__(self, reasons: list[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(reasons))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize(text: str) -> str:
    """Case/accent-insensitive comparison key.  Never used for display."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def _clean(value: str, label: str, limit: int, required: bool = True) -> str:
    value = (value or "").strip()
    if required and not value:
        raise ValidationError(f"{label} é obrigatório.")
    if len(value) > limit:
        raise ValidationError(f"{label} excede {limit} caracteres.")
    if any(c in value for c in ("\x00", "\r")):
        raise ValidationError(f"{label} contém caracteres inválidos.")
    return value


def parse_moment(raw: str, label: str = "data") -> str:
    """Parse an ISO-8601 local datetime (no timezone required) into canonical form."""
    raw = (raw or "").strip()
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(
            f"{label} inválida: use AAAA-MM-DDTHH:MM (ex.: 2026-11-10T19:00)."
        ) from exc
    if dt.tzinfo is not None:
        raise ValidationError(f"{label} deve ser horário local, sem fuso.")
    return dt.replace(second=0, microsecond=0).isoformat()


def is_http_url(raw: str) -> bool:
    return bool(re.match(r"^https?://[^\s<>\"']+$", raw or "")) and len(raw) <= MAX_URL


@dataclass(frozen=True)
class Event:
    title: str
    venue: str
    starts_at: str
    submitted_by: int
    description: str = ""
    ends_at: Optional[str] = None
    source_url: str = ""
    # Escopo declarado do canal (Agenda Cultural *Salvador*). Não é inferência
    # de IA: o bot só opera neste município, e o portão de publicação exige
    # estes dois campos preenchidos no banco canônico.
    municipio: str = "Salvador"
    uf: str = "BA"
    status: EventStatus = EventStatus.PENDING
    authorization_confirmed: bool = False
    authorization_reference: str = ""
    id: Optional[int] = None
    published_at: Optional[str] = None
    publish_reference: Optional[str] = None
    # Motivo editorial persistido em ``eventos.motivo``: na ingestão é o que o
    # núcleo apontou como pendência; na rejeição é o motivo escrito pelo admin.
    motivo: str = ""
    # Campos que a coleta já grava no núcleo canônico. O bot os lê para os
    # filtros públicos (/gratis, /bairro, /musica...) e os corrige por
    # /corrigir; ``add``/``update`` do repositório continuam sem sobrescrevê-los
    # quando o Event vem de um caminho que não os conhece.
    bairro: str = ""
    categoria: str = ""
    preco: str = ""
    endereco: str = ""
    # Link de venda/ingresso (Bilheteria Digital, Sympla, etc.). Distinto de
    # source_url (a página da fonte onde o evento foi coletado).
    url_ingresso: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def validated(self) -> "Event":
        title = _clean(self.title, "Título", MAX_TITLE)
        # Local pode faltar em evento coletado (ex.: calendário do TCA não
        # publica sala). A moderação precisa funcionar sem ele; o portão de
        # publicação é quem exige local comprovado.
        venue = _clean(self.venue, "Local", MAX_VENUE, required=False)
        starts_at = parse_moment(self.starts_at, "Data de início")
        ends_at = parse_moment(self.ends_at, "Data de término") if self.ends_at else None
        if ends_at and ends_at < starts_at:
            raise ValidationError("A data de término é anterior à de início.")
        description = _clean(self.description, "Descrição", MAX_DESCRIPTION, required=False)
        source_url = _clean(self.source_url, "Link da fonte", MAX_URL, required=False)
        if source_url and not is_http_url(source_url):
            raise ValidationError("Link da fonte deve começar com http:// ou https://.")
        url_ingresso = _clean(self.url_ingresso, "Link do ingresso", MAX_URL, required=False)
        if url_ingresso and not is_http_url(url_ingresso):
            raise ValidationError("Link do ingresso deve começar com http:// ou https://.")
        auth_ref = _clean(
            self.authorization_reference, "Referência da autorização", 300, required=False
        )
        municipio = _clean(self.municipio, "Município", 120, required=False)
        uf = _clean(self.uf, "UF", 2, required=False).upper()
        bairro = _clean(self.bairro, "Bairro", MAX_BAIRRO, required=False)
        categoria = _clean(self.categoria, "Categoria", MAX_CATEGORIA, required=False)
        preco = _clean(self.preco, "Preço", MAX_PRECO, required=False)
        endereco = _clean(self.endereco, "Endereço", MAX_ENDERECO, required=False)
        if not isinstance(self.submitted_by, int) or self.submitted_by < 0:
            raise ValidationError("Autor inválido.")
        # submitted_by == 0 significa "ingerido pelo coletor": não há operador
        # humano associado, mas o evento continua moderável.
        return Event(
            title=title,
            venue=venue,
            starts_at=starts_at,
            ends_at=ends_at,
            description=description,
            source_url=source_url,
            submitted_by=self.submitted_by,
            municipio=municipio,
            uf=uf,
            status=self.status,
            authorization_confirmed=self.authorization_confirmed,
            authorization_reference=auth_ref,
            id=self.id,
            published_at=self.published_at,
            publish_reference=self.publish_reference,
            motivo=self.motivo,
            bairro=bairro,
            categoria=categoria,
            preco=preco,
            endereco=endereco,
            created_at=self.created_at,
            updated_at=now_iso(),
        )

    @property
    def dedupe_key(self) -> str:
        """Identity used to refuse accidental duplicates."""
        raw = "|".join(
            [normalize(self.title), normalize(self.venue), self.starts_at[:16]]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    @property
    def content_hash(self) -> str:
        raw = "|".join(
            [
                normalize(self.title),
                normalize(self.venue),
                self.starts_at,
                normalize(self.description),
                self.source_url,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def authorization_gaps(self) -> list[str]:
        """Reasons this event may not be published yet (empty == publishable)."""
        gaps: list[str] = []
        if self.status not in (EventStatus.APPROVED, EventStatus.PUBLISHED):
            gaps.append("evento não aprovado por administrador")
        if not self.authorization_confirmed:
            gaps.append("autorização do organizador não confirmada")
        if not self.authorization_reference.strip():
            gaps.append("referência da autorização ausente")
        if not is_http_url(self.source_url):
            gaps.append("link de fonte verificável ausente")
        if not self.description.strip():
            gaps.append("descrição ausente")
        return gaps


@dataclass(frozen=True)
class PublicEventView:
    """What the public may see.  Deliberately omits internal auth references."""

    id: int
    title: str
    venue: str
    starts_at: str
    ends_at: Optional[str]
    description: str
    source_url: str

    @classmethod
    def from_event(cls, event: Event) -> "PublicEventView":
        assert event.id is not None
        return cls(
            id=event.id,
            title=event.title,
            venue=event.venue,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            description=event.description,
            source_url=event.source_url,
        )
