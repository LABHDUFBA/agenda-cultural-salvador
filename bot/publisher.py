"""Conservative publication.

Rules, in order, all enforced here on the server:

1. Only an *admin* (role read from SQLite) may trigger publication.
2. The event must be approved, have a confirmed organiser authorisation, a
   reference for that authorisation, a verifiable source URL, and a description.
3. Publication is **dry-run by default**.  Sending needs three independent
   opt-ins: ``dry_run=False``, an operator-typed confirmation phrase, and the
   deployment flag ``allow_execute=True``.
4. Rate limits: minimum interval between sends and a hard cap per batch.
5. Idempotent: an already-published event is skipped, never re-sent.
6. Every decision (including refusals) is written to ``publish_attempts`` and
   ``audit_log``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import (
    AuditRepository,
    EventRepository,
    PublishAttemptRepository,
)
from database.models import para_dt
from .models import (
    Event,
    EventStatus,
    PublishBlocked,
    UserRole,
    now_iso,
)
from .permissions import PermissionService
from .telegram_api import Transport

CONFIRMATION_PHRASE = "PUBLICAR"


@dataclass(frozen=True)
class PublisherConfig:
    target_chat_id: Optional[int] = None
    allow_execute: bool = False          # deployment-level kill switch
    max_per_run: int = 3                 # hard cap per batch
    min_interval_seconds: int = 300      # spacing between real sends
    max_event_age_hours: int = 0         # 0 = evento no passado é bloqueado


@dataclass(frozen=True)
class PublishResult:
    event_id: int
    outcome: str            # dry_run | sent | skipped | blocked
    detail: str = ""
    message_id: Optional[str] = None
    preview: str = ""


def _friendly(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y às %H:%M")
    except ValueError:
        return iso


def format_publication(event: Event) -> str:
    """Public text.  Plain text only (no parse_mode) so user input cannot inject HTML."""
    when = _friendly(event.starts_at)
    if event.ends_at:
        when += f" até {_friendly(event.ends_at)}"
    parts = [event.title, "", f"{when}", f"{event.venue}", "", event.description]
    if event.source_url:
        parts += ["", f"Fonte: {event.source_url}"]
    return "\n".join(parts).strip()


class Publisher:
    def __init__(
        self,
        events: EventRepository,
        attempts: PublishAttemptRepository,
        audit: AuditRepository,
        permissions: PermissionService,
        transport: Transport,
        config: Optional[PublisherConfig] = None,
        clock=None,
    ):
        self._events = events
        self._attempts = attempts
        self._audit = audit
        self._perms = permissions
        self._transport = transport
        self._config = config or PublisherConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def config(self) -> PublisherConfig:
        return self._config

    # -- internal ---------------------------------------------------------
    def _blockers(self, event: Event) -> list[str]:
        blockers = list(event.authorization_gaps())
        # Evidências canônicas (Salvador, data local coerente, local, fonte
        # verificável). Só o repositório canônico sabe provar isso; se o
        # repositório injetado não expõe ``publication_gaps``, a resposta é
        # BLOQUEAR (fail-closed). Antes a ausência do método era ignorada e o
        # evento passava só com os campos do domínio — um gate que desaparece
        # quando a dependência não o implementa não é um gate.
        gaps = getattr(self._events, "publication_gaps", None)
        if gaps is None:
            if "portao_canonico_indisponivel" not in blockers:
                blockers.append("portao_canonico_indisponivel")
        else:
            for motivo in gaps(event.id):
                if motivo not in blockers:
                    blockers.append(motivo)
        # ``starts_at`` é horário local de Salvador sem fuso (é assim que o bot
        # recebe o comando). Interpretá-lo como UTC adiantava o evento em três
        # horas e bloqueava como "já ocorreu" um evento que ainda ia começar.
        try:
            starts = para_dt(event.starts_at)
        except (ValueError, TypeError):
            blockers.append("data de início inválida")
        else:
            if starts < self._clock() - timedelta(hours=self._config.max_event_age_hours):
                blockers.append("evento já ocorreu")
        return blockers

    def _record(
        self,
        event_id: int,
        mode: str,
        outcome: str,
        detail: str,
        actor_id: Optional[int],
        referencia: str = "",
    ) -> None:
        self._attempts.record(event_id, mode, outcome, detail, referencia)
        self._audit.log(
            actor_id=actor_id,
            action=f"publish.{mode}",
            entity="event",
            entity_id=str(event_id),
            outcome="allowed" if outcome in ("sent", "dry_run") else outcome,
            detail=detail,
        )

    # -- public API -------------------------------------------------------
    def plan(self, event_id: int) -> list[str]:
        """Read-only: what would stop this event from being published?"""
        try:
            event = self._events.get(event_id)
        except Exception as exc:  # NotFound
            return [str(exc)]
        if event.status is EventStatus.PUBLISHED:
            return []
        return self._blockers(event)

    def publish(
        self,
        event_id: int,
        actor_id: Optional[int],
        dry_run: bool = True,
        confirmation: Optional[str] = None,
    ) -> PublishResult:
        self._perms.require(actor_id, UserRole.ADMIN, "publish.request")
        event = self._events.get(event_id)

        if event.status is EventStatus.PUBLISHED:
            self._record(event.id, "dry_run" if dry_run else "execute", "skipped",
                         "já publicado", actor_id)
            return PublishResult(event.id, "skipped", "Evento já publicado.",
                                 event.publish_reference, format_publication(event))

        blockers = self._blockers(event)
        if blockers:
            detail = "; ".join(blockers)
            self._record(event.id, "dry_run" if dry_run else "execute", "blocked", detail, actor_id)
            raise PublishBlocked(blockers)

        preview = format_publication(event)

        if dry_run:
            self._record(event.id, "dry_run", "dry_run",
                         f"simulação para chat {self._config.target_chat_id or 'não configurado'}",
                         actor_id)
            return PublishResult(event.id, "dry_run",
                                 "Simulação concluída. Nada foi enviado.", None, preview)

        if not self._config.allow_execute:
            detail = "execução real desativada neste ambiente"
            self._record(event.id, "execute", "blocked", detail, actor_id)
            raise PublishBlocked([detail])
        if (confirmation or "").strip() != CONFIRMATION_PHRASE:
            detail = "frase de confirmação ausente ou incorreta"
            self._record(event.id, "execute", "blocked", detail, actor_id)
            raise PublishBlocked([detail])
        if self._config.target_chat_id is None:
            detail = "canal de publicação não configurado"
            self._record(event.id, "execute", "blocked", detail, actor_id)
            raise PublishBlocked([detail])

        last = self._attempts.last_executed_at()
        if last:
            elapsed = self._clock() - datetime.fromisoformat(last.replace("Z", "+00:00"))
            if elapsed < timedelta(seconds=self._config.min_interval_seconds):
                detail = (
                    f"intervalo mínimo de {self._config.min_interval_seconds}s entre envios"
                )
                self._record(event.id, "execute", "blocked", detail, actor_id)
                raise PublishBlocked([detail])

        try:
            message_id = self._transport.send_event(event, self._config.target_chat_id)
        except Exception as exc:
            self._record(event.id, "execute", "failed", f"transporte: {exc}", actor_id)
            raise PublishBlocked([f"falha no envio: {exc}"]) from exc

        # O fato (envio) é registrado ANTES de mover o status: "publicado" é
        # derivado de tentativas_publicacao, então um crash entre as duas
        # escritas não pode deixar no canal um evento que o banco não viu.
        self._record(event.id, "execute", "sent", f"message_id={message_id}", actor_id,
                     referencia=message_id)
        try:
            updated = Event(**{**event.__dict__,
                               "status": EventStatus.PUBLISHED,
                               "published_at": now_iso(),
                               "publish_reference": message_id})
            self._events.update(updated)
        except Exception as exc:  # enviado, mas o status não acompanhou
            self._audit.log(
                actor_id, "publish.state_desync", "event", str(event.id), "failed",
                f"enviado msg={message_id}; status não atualizado: {exc}"[:300],
            )
            return PublishResult(event.id, "sent",
                                 "Publicado; status será reconciliado pelo registro de tentativas.",
                                 message_id, preview)
        return PublishResult(event.id, "sent", "Publicado.", message_id, preview)

    def publish_pending(
        self,
        actor_id: Optional[int],
        dry_run: bool = True,
        confirmation: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[PublishResult]:
        """Batch mode, still capped and still dry-run by default."""
        self._perms.require(actor_id, UserRole.ADMIN, "publish.batch")
        cap = min(limit or self._config.max_per_run, self._config.max_per_run)
        queue = self._events.list_by_status([EventStatus.APPROVED], limit=50)
        results: list[PublishResult] = []
        for event in queue[:cap]:
            try:
                results.append(self.publish(event.id, actor_id, dry_run, confirmation))
            except PublishBlocked as exc:
                results.append(PublishResult(event.id, "blocked", "; ".join(exc.reasons)))
        return results

    def status(self) -> dict:
        return {
            "dry_run_default": True,
            "allow_execute": self._config.allow_execute,
            "target_chat_id": self._config.target_chat_id,
            "max_per_run": self._config.max_per_run,
            "min_interval_seconds": self._config.min_interval_seconds,
        }
