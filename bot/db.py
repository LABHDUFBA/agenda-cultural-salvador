"""Storage layer: narrow repository interfaces + a SQLite implementation.

There is exactly **one** database and one schema: ``database/schema.sql``
(owned by the canonical core). This module no longer creates a private
``events`` table; it adapts the bot's domain model onto the canonical
``eventos`` rows, and keeps Telegram-specific facts (operators, publication
attempts) in ``usuarios`` / ``tentativas_publicacao`` inside the same schema.

Mapping decisions (single source of truth):

* editorial status lives in ``eventos.status`` (pendente/aprovado/rejeitado/
  duplicado). Collectors never approve; only a human admin can move a row to
  ``aprovado``.
* "published" is **not** a status. It is derived from
  ``tentativas_publicacao`` (modo='execute', resultado='sent'), so approving
  and publishing cannot disagree.
* dates are stored in UTC by the canonical core; the bot displays and accepts
  naive America/Bahia local time (converted symmetrically here).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence

from database.connection import transacao
from database.dedup import chave_deduplicacao, data_ref_local, normalizar_texto
from database.models import FUSO_BAHIA, agora_utc, para_dt, para_iso
from database.repository import requisitos_publicacao_evento
from database.connection import conectar as _conectar_canonico
from database.connection import inicializar as _inicializar_canonico

from .models import (
    DuplicateEvent,
    Event,
    EventStatus,
    NotFound,
    UserRole,
    ValidationError,
    now_iso,
)

# --------------------------------------------------------------------------
# status mapping
# --------------------------------------------------------------------------
_STATUS_PARA_CANONICO: dict[EventStatus, str] = {
    EventStatus.DRAFT: "pendente",
    EventStatus.PENDING: "pendente",
    EventStatus.APPROVED: "aprovado",
    EventStatus.PUBLISHED: "aprovado",
    EventStatus.REJECTED: "rejeitado",
    EventStatus.DUPLICATE: "duplicado",
}

_STATUS_DO_CANONICO: dict[str, EventStatus] = {
    "pendente": EventStatus.PENDING,
    "aprovado": EventStatus.APPROVED,
    "rejeitado": EventStatus.REJECTED,
    "duplicado": EventStatus.DUPLICATE,
}

_SENT = "EXISTS (SELECT 1 FROM tentativas_publicacao t WHERE t.evento_id = e.id"
_SENT += " AND t.modo = 'execute' AND t.resultado = 'sent')"

# Correção administrativa: campo de comando -> atributo do Event.
CAMPOS_CORRIGIVEIS: dict[str, str] = {
    "titulo": "title",
    "local": "venue",
    "inicio": "starts_at",
    "fim": "ends_at",
    "descricao": "description",
    "url": "source_url",
    "bairro": "bairro",
    "categoria": "categoria",
    "preco": "preco",
    "endereco": "endereco",
}

# Campo de comando -> coluna canônica (para antes/depois na auditoria).
CAMPOS_CANONICOS: dict[str, str] = {
    "titulo": "titulo",
    "local": "local_nome",
    "inicio": "inicio_em",
    "fim": "fim_em",
    "descricao": "descricao",
    "url": "url_fonte",
    "bairro": "bairro",
    "categoria": "categoria",
    "preco": "preco",
    "endereco": "endereco",
}


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor_id: Optional[int]
    action: str
    entity: str
    entity_id: Optional[str]
    outcome: str
    detail: str
    created_at: str


@dataclass(frozen=True)
class PublishAttempt:
    id: int
    event_id: int
    mode: str
    outcome: str
    detail: str
    created_at: str


class EventRepository(Protocol):
    def add(self, event: Event) -> Event: ...

    def get(self, event_id: int) -> Event: ...

    def update(self, event: Event) -> Event: ...

    def reject(self, event_id: int, actor_id, reason: str) -> Event: ...

    def list_by_status(
        self, statuses: Sequence[EventStatus], limit: int = 20, offset: int = 0
    ) -> list[Event]: ...

    def list_upcoming(self, limit: int = 20, offset: int = 0) -> list[Event]: ...

    def list_public(
        self,
        *,
        de: Optional[str] = None,
        ate: Optional[str] = None,
        depois_de: Optional[str] = None,
        bairro: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Event]: ...

    def bairros(self, limit: int = 20) -> list[tuple[str, int]]: ...

    def fontes(self, limit: int = 40) -> list[dict]: ...

    def corrigir(self, event_id: int, changes: dict[str, str], actor_id) -> Event: ...

    def search(self, term: str, limit: int = 20) -> list[Event]: ...

    def count_by_status(self) -> dict[str, int]: ...


class UserRepository(Protocol):
    def get_role(self, telegram_id: int) -> UserRole: ...

    def upsert(self, telegram_id: int, username: str, role: UserRole) -> None: ...

    def list_all(self, limit: int = 50) -> list[tuple[int, str, UserRole]]: ...


class AuditRepository(Protocol):
    def log(
        self,
        actor_id: Optional[int],
        action: str,
        entity: str,
        entity_id: Optional[str] = None,
        outcome: str = "info",
        detail: str = "",
    ) -> None: ...

    def for_entity(self, entity: str, entity_id: str) -> list[AuditEntry]: ...

    def recent(self, limit: int = 50) -> list[AuditEntry]: ...


class PublishAttemptRepository(Protocol):
    def record(
        self, event_id: int, mode: str, outcome: str, detail: str = "", referencia: str = ""
    ) -> None: ...

    def list_for_event(self, event_id: int) -> list[PublishAttempt]: ...

    def last_executed_at(self) -> Optional[str]: ...

    def executed_count_since(self, since_iso: str) -> int: ...


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the canonical database (creates parent dirs, applies schema)."""
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return _conectar_canonico(path)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create/migrate the canonical schema. Idempotent, never destructive."""
    _inicializar_canonico(conn)


# --------------------------------------------------------------------------
# conversions
# --------------------------------------------------------------------------
def _local_naive(iso_utc: str) -> str:
    """UTC ISO -> naive America/Bahia 'AAAA-MM-DDTHH:MM' (what the bot shows)."""
    momento = para_dt(iso_utc).astimezone(FUSO_BAHIA).replace(
        tzinfo=None, second=0, microsecond=0
    )
    return momento.isoformat(timespec="minutes")


def _published_fields(conn: sqlite3.Connection, event_id: int) -> tuple[Optional[str], Optional[str]]:
    row = conn.execute(
        "SELECT criado_em, referencia FROM tentativas_publicacao "
        "WHERE evento_id = ? AND modo = 'execute' AND resultado = 'sent' "
        "ORDER BY id DESC LIMIT 1",
        (event_id,),
    ).fetchone()
    if row is None:
        return None, None
    return row["criado_em"], (row["referencia"] or None)


def _row_to_event(conn: sqlite3.Connection, row: sqlite3.Row) -> Event:
    published_at, publish_reference = _published_fields(conn, row["id"])
    status = _STATUS_DO_CANONICO.get(row["status"], EventStatus.PENDING)
    if status is EventStatus.APPROVED and published_at is not None:
        status = EventStatus.PUBLISHED
    return Event(
        id=row["id"],
        title=row["titulo"],
        venue=row["local_nome"],
        starts_at=_local_naive(row["inicio_em"]),
        ends_at=_local_naive(row["fim_em"]) if row["fim_em"] else None,
        description=row["descricao"],
        source_url=row["url_fonte"],
        submitted_by=row["enviado_por"],
        municipio=row["municipio"],
        uf=row["uf"],
        bairro=row["bairro"],
        categoria=row["categoria"],
        preco=row["preco"],
        endereco=row["endereco"],
        url_ingresso=row["url_ingresso"] if "url_ingresso" in row.keys() else "",
        status=status,
        authorization_confirmed=bool(row["autorizacao_confirmada"]),
        authorization_reference=row["autorizacao_referencia"],
        published_at=published_at,
        publish_reference=publish_reference,
        motivo=row["motivo"],
        created_at=row["criado_em"],
        updated_at=row["atualizado_em"],
    )


class SqliteEventRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # -- helpers ----------------------------------------------------------
    def _row(self, event_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM eventos WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"Evento {event_id} não encontrado.")
        return row

    def add(self, event: Event) -> Event:
        event = event.validated()
        if event.status is EventStatus.PUBLISHED:
            # "Publicado" é fato externo: só o Publisher, com as cinco travas,
            # cria a linha em tentativas_publicacao. Um add() que gravasse
            # status=published fabricaria um envio que nunca aconteceu — e,
            # como o status publicado é derivado dessa tabela, o evento
            # apareceria como publicado para todo mundo.
            raise ValidationError(
                "Evento não pode ser criado já publicado; use o Publisher."
            )
        inicio = para_iso(event.starts_at)
        fim = para_iso(event.ends_at) if event.ends_at else None
        agora = para_iso(agora_utc())
        chave = chave_deduplicacao(
            titulo=event.title,
            inicio_em=inicio,
            local_nome=event.venue,
            municipio=event.municipio or "Salvador",
        )
        status = _STATUS_PARA_CANONICO[event.status]
        try:
            with transacao(self._conn):
                cur = self._conn.execute(
                    "INSERT INTO eventos ("
                    "titulo, titulo_normalizado, descricao, inicio_em, fim_em, data_ref, "
                    "local_nome, local_normalizado, endereco, bairro, municipio, uf, "
                    "categoria, url_ingresso, preco, status, motivo, chave_dedup, "
                    "criado_em, atualizado_em, url_fonte, enviado_por, "
                    "autorizacao_confirmada, autorizacao_referencia"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.title,
                        normalizar_texto(event.title),
                        event.description,
                        inicio,
                        fim,
                        data_ref_local(inicio),
                        event.venue,
                        normalizar_texto(event.venue),
                        event.endereco,
                        event.bairro,
                        event.municipio,
                        event.uf,
                        event.categoria,
                        event.url_ingresso,
                        event.preco,
                        status,
                        "",
                        chave,
                        agora,
                        agora,
                        event.source_url,
                        event.submitted_by,
                        1 if event.authorization_confirmed else 0,
                        event.authorization_reference,
                    ),
                )
                event_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise DuplicateEvent(
                "Já existe um evento com mesmo título, local e horário."
            ) from exc
        return self.get(event_id)

    def get(self, event_id: int) -> Event:
        return _row_to_event(self._conn, self._row(event_id))

    def update(self, event: Event) -> Event:
        if event.id is None:
            raise NotFound("Evento sem identificador.")
        event = event.validated()
        atual = self._row(event.id)
        destino = _STATUS_PARA_CANONICO[event.status]
        if atual["status"] == "rejeitado" and destino != "rejeitado":
            raise ValidationError("Evento rejeitado não pode voltar à fila.")
        if atual["status"] == "duplicado" and destino != "duplicado":
            raise ValidationError("Evento duplicado é registro histórico e não muda de situação.")
        inicio = para_iso(event.starts_at)
        fim = para_iso(event.ends_at) if event.ends_at else None
        chave = chave_deduplicacao(
            titulo=event.title,
            inicio_em=inicio,
            local_nome=event.venue,
            municipio=event.municipio or "Salvador",
        )
        try:
            with transacao(self._conn):
                cur = self._conn.execute(
                    "UPDATE eventos SET titulo=?, titulo_normalizado=?, descricao=?, "
                    "inicio_em=?, fim_em=?, data_ref=?, local_nome=?, local_normalizado=?, "
                    "municipio=?, uf=?, status=?, chave_dedup=?, atualizado_em=?, "
                    "url_fonte=?, autorizacao_confirmada=?, autorizacao_referencia=? "
                    "WHERE id=?",
                    (
                        event.title,
                        normalizar_texto(event.title),
                        event.description,
                        inicio,
                        fim,
                        data_ref_local(inicio),
                        event.venue,
                        normalizar_texto(event.venue),
                        event.municipio,
                        event.uf,
                        destino,
                        chave,
                        para_iso(agora_utc()),
                        event.source_url,
                        1 if event.authorization_confirmed else 0,
                        event.authorization_reference,
                        event.id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEvent(
                "Já existe um evento com mesmo título, local e horário."
            ) from exc
        if cur.rowcount != 1:
            raise NotFound(f"Evento {event.id} não encontrado.")
        # Publicação é registrada pelo Publisher em tentativas_publicacao
        # (fonte única do fato); update() só move o status editorial.
        return self.get(event.id)

    def reject(self, event_id: int, actor_id, reason: str) -> Event:
        """Rejeita preservando o registro, a proveniência e o motivo.

        Grava ``eventos.motivo`` com o motivo humano e deixa o motivo anterior
        (o que a ingestão apontou como pendência) na auditoria: a trilha é
        append-only, então nada do que explicava o evento se perde. O status
        não pode ir para ``rejeitado`` sem motivo — rejeição sem justificativa
        não é auditável.
        """
        motivo = (reason or "").strip()
        if not motivo:
            raise ValidationError("Informe o motivo da rejeição.")
        with transacao(self._conn):
            atual = self._row(event_id)
            if atual["status"] == "duplicado":
                raise ValidationError(
                    "Evento duplicado é registro histórico e não muda de situação."
                )
            if atual["status"] == "rejeitado":
                raise ValidationError(f"Evento {event_id} já está rejeitado.")
            if self._is_published(event_id):
                # Já saiu no canal: "rejeitar" aqui seria reescrever um fato
                # externo. O caminho é registrar a correção/retratação, não
                # fingir que nunca foi publicado.
                raise ValidationError(
                    f"Evento {event_id} já foi publicado; rejeição não se aplica."
                )
            anterior = atual["motivo"] or ""
            self._conn.execute(
                "UPDATE eventos SET status = 'rejeitado', motivo = ?, atualizado_em = ? "
                "WHERE id = ?",
                (motivo[:1000], para_iso(agora_utc()), event_id),
            )
            detalhe = motivo[:400]
            if anterior and anterior != motivo:
                detalhe += f" | motivo_anterior: {anterior[:400]}"
            self._conn.execute(
                "INSERT INTO auditoria (entidade, entidade_id, acao, estado_anterior, "
                "estado_novo, ator, detalhe, ocorrido_em) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "evento",
                    event_id,
                    "event.reject",
                    atual["status"],
                    "rejeitado",
                    str(actor_id) if actor_id else "sistema",
                    detalhe[:500],
                    now_iso(),
                ),
            )
        return self.get(event_id)

    def _is_published(self, event_id: int) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM tentativas_publicacao WHERE evento_id = ? "
                "AND modo = 'execute' AND resultado = 'sent' LIMIT 1",
                (event_id,),
            ).fetchone()
            is not None
        )

    # -- publication gate -------------------------------------------------
    def publication_gaps(self, event_id: int) -> list[str]:
        """Canonical evidence still missing before this event may be published."""
        return list(requisitos_publicacao_evento(self._conn, event_id))

    # -- listings ---------------------------------------------------------
    def list_by_status(
        self, statuses: Sequence[EventStatus], limit: int = 20, offset: int = 0
    ) -> list[Event]:
        if not statuses:
            return []
        wanted = set(statuses)
        clauses: list[str] = []
        params: list[object] = []
        simples = {
            _STATUS_PARA_CANONICO[s]
            for s in wanted
            if s not in (EventStatus.APPROVED, EventStatus.PUBLISHED)
        }
        if simples:
            clauses.append("e.status IN (" + ",".join("?" for _ in simples) + ")")
            params.extend(sorted(simples))
        if EventStatus.APPROVED in wanted:
            clauses.append("(e.status = 'aprovado' AND NOT " + _SENT + ")")
        if EventStatus.PUBLISHED in wanted:
            clauses.append("(e.status = 'aprovado' AND " + _SENT + ")")
        if not clauses:
            return []
        sql = (
            "SELECT e.* FROM eventos e WHERE " + " OR ".join(clauses)
            + " ORDER BY e.inicio_em ASC, e.id ASC LIMIT ? OFFSET ?"
        )
        rows = self._conn.execute(
            sql, (*params, max(1, limit), max(0, offset))
        ).fetchall()
        return [_row_to_event(self._conn, r) for r in rows]

    def list_upcoming(
        self, limit: int = 20, offset: int = 0, now: Optional[str] = None
    ) -> list[Event]:
        agora_iso = para_iso(now) if now else para_iso(agora_utc())
        rows = self._conn.execute(
            "SELECT e.* FROM eventos e WHERE e.status = 'aprovado' AND e.inicio_em >= ? "
            "ORDER BY e.inicio_em ASC, e.id ASC LIMIT ? OFFSET ?",
            (agora_iso, max(1, limit), max(0, offset)),
        ).fetchall()
        return [_row_to_event(self._conn, r) for r in rows]

    # -- filtros públicos ------------------------------------------------
    def list_public(
        self,
        *,
        de: Optional[str] = None,
        ate: Optional[str] = None,
        depois_de: Optional[str] = None,
        bairro: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Event]:
        """Eventos aprovados no intervalo de datas locais (``data_ref``).

        ``de``/``ate`` são datas locais (AAAA-MM-DD) comparadas à coluna
        canônica ``data_ref`` — o mesmo dia que o público vê em Salvador.
        ``depois_de`` é um instante UTC ISO para cortar o que já passou.
        ``bairro`` é comparado sem acento/maiúsculas em Python, porque o
        schema canônico não tem coluna normalizada de bairro.
        """
        clauses = ["e.status = 'aprovado'"]
        params: list[object] = []
        if de:
            clauses.append("e.data_ref >= ?")
            params.append(de)
        if ate:
            clauses.append("e.data_ref <= ?")
            params.append(ate)
        if depois_de:
            clauses.append("e.inicio_em >= ?")
            params.append(para_iso(depois_de))
        where = " AND ".join(clauses)
        if bairro:
            # Filtro textual exige varrer candidatos do período; o volume
            # público é pequeno e o recorte de datas continua no SQL.
            rows = self._conn.execute(
                f"SELECT e.* FROM eventos e WHERE {where} "
                "ORDER BY e.inicio_em ASC, e.id ASC LIMIT 500",
                params,
            ).fetchall()
            alvo = normalizar_texto(bairro)
            eventos = [
                e
                for e in (_row_to_event(self._conn, r) for r in rows)
                if alvo in normalizar_texto(e.bairro)
            ]
            return eventos[max(0, offset): max(0, offset) + max(1, limit)]
        rows = self._conn.execute(
            f"SELECT e.* FROM eventos e WHERE {where} "
            "ORDER BY e.inicio_em ASC, e.id ASC LIMIT ? OFFSET ?",
            (*params, max(1, limit), max(0, offset)),
        ).fetchall()
        return [_row_to_event(self._conn, r) for r in rows]

    def bairros(self, limit: int = 20) -> list[tuple[str, int]]:
        """Bairros com eventos aprovados, do mais movimentado ao menos."""
        rows = self._conn.execute(
            "SELECT bairro, COUNT(*) AS n FROM eventos "
            "WHERE status = 'aprovado' AND trim(bairro) <> '' "
            "GROUP BY bairro ORDER BY n DESC, bairro ASC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [(r["bairro"], int(r["n"])) for r in rows]

    def fontes(self, limit: int = 40) -> list[dict]:
        """Catálogo canônico de fontes + eventos aprovados e última coleta.

        Somente leitura. A contagem só considera eventos com proveniência
        daquela fonte (a mesma evidência que o portão de publicação usa).
        """
        rows = self._conn.execute(
            "SELECT f.id, f.nome, f.url_base, f.tipo, f.confiabilidade, "
            "  (SELECT COUNT(*) FROM eventos e "
            "     WHERE e.status = 'aprovado' AND EXISTS ("
            "       SELECT 1 FROM proveniencia p "
            "       WHERE p.evento_id = e.id AND p.fonte_id = f.id)) AS aprovados, "
            "  (SELECT MAX(s.iniciada_em) FROM sessoes s "
            "     WHERE s.fonte_id = f.id) AS ultima_coleta, "
            "  (SELECT s.status FROM sessoes s WHERE s.fonte_id = f.id "
            "     ORDER BY s.id DESC LIMIT 1) AS ultimo_status "
            "FROM fontes f ORDER BY f.nome ASC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "nome": r["nome"],
                "url_base": r["url_base"],
                "tipo": r["tipo"],
                "confiabilidade": r["confiabilidade"],
                "aprovados": int(r["aprovados"]),
                "ultima_coleta": r["ultima_coleta"],
                "ultimo_status": r["ultimo_status"],
            }
            for r in rows
        ]

    # -- correção administrativa ------------------------------------------
    def corrigir(self, event_id: int, changes: dict[str, str], actor_id) -> Event:
        """Corrige campos editoriais sem mudar status nem republicar.

        Campos aceitos são apenas dados do evento (nunca ``status``,
        autorização ou proveniência). Recalcula as colunas derivadas
        (normalizadas, ``data_ref`` e chave de deduplicação) e registra
        antes/depois na auditoria append-only.
        """
        if not changes:
            raise ValidationError("Informe ao menos um campo para corrigir.")
        desconhecidos = sorted(set(changes) - set(CAMPOS_CORRIGIVEIS))
        if desconhecidos:
            raise ValidationError(
                "Campo não corrigível: " + ", ".join(desconhecidos)
                + ". Use: " + ", ".join(sorted(CAMPOS_CORRIGIVEIS))
            )
        atual_row = self._row(event_id)
        if atual_row["status"] == "duplicado":
            raise ValidationError(
                "Evento duplicado é registro histórico e não pode ser corrigido."
            )
        atual = _row_to_event(self._conn, atual_row)
        atualizados = dict(atual.__dict__)
        for campo, valor in changes.items():
            atualizados[CAMPOS_CORRIGIVEIS[campo]] = valor
        novo = Event(**atualizados).validated()

        inicio = para_iso(novo.starts_at)
        fim = para_iso(novo.ends_at) if novo.ends_at else None
        chave = chave_deduplicacao(
            titulo=novo.title,
            inicio_em=inicio,
            local_nome=novo.venue,
            municipio=novo.municipio or "Salvador",
        )
        antes = {
            col: str(atual_row[col]) if atual_row[col] is not None else ""
            for col in CAMPOS_CANONICOS.values()
        }
        try:
            with transacao(self._conn):
                self._conn.execute(
                    "UPDATE eventos SET titulo=?, titulo_normalizado=?, descricao=?, "
                    "inicio_em=?, fim_em=?, data_ref=?, local_nome=?, "
                    "local_normalizado=?, endereco=?, bairro=?, categoria=?, "
                    "preco=?, url_fonte=?, chave_dedup=?, atualizado_em=? WHERE id=?",
                    (
                        novo.title,
                        normalizar_texto(novo.title),
                        novo.description,
                        inicio,
                        fim,
                        data_ref_local(inicio),
                        novo.venue,
                        normalizar_texto(novo.venue),
                        novo.endereco,
                        novo.bairro,
                        novo.categoria,
                        novo.preco,
                        novo.source_url,
                        chave,
                        para_iso(agora_utc()),
                        event_id,
                    ),
                )
                depois = {c: str(novo_val) for c, novo_val in {
                    "titulo": novo.title, "local_nome": novo.venue,
                    "inicio_em": inicio, "fim_em": fim or "",
                    "descricao": novo.description, "url_fonte": novo.source_url,
                    "bairro": novo.bairro, "categoria": novo.categoria,
                    "preco": novo.preco, "endereco": novo.endereco,
                }.items()}
                detalhe = "; ".join(
                    f"{campo}: {antes[col][:80]!r} -> {depois[col][:80]!r}"
                    for campo, col in CAMPOS_CANONICOS.items()
                    if campo in changes
                )
                self._conn.execute(
                    "INSERT INTO auditoria (entidade, entidade_id, acao, "
                    "estado_anterior, estado_novo, ator, detalhe, ocorrido_em) "
                    "VALUES ('event', ?, 'event.correct', ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        antes["titulo"][:200],
                        depois["titulo"][:200],
                        str(actor_id) if actor_id else "sistema",
                        detalhe[:500],
                        now_iso(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEvent(
                "A correção colidiria com outro evento (mesmo título, local e horário)."
            ) from exc
        return self.get(event_id)

    def search(self, term: str, limit: int = 20) -> list[Event]:
        needle = normalizar_texto(term)
        if not needle:
            return []
        # ``%`` e ``_`` são curingas do LIKE: sem escape, "/buscar %" varria a
        # tabela inteira em vez de procurar o caractere.
        like_norm = f"%{_escapar_like(needle)}%"
        like_raw = f"%{_escapar_like(term.lower())}%"
        rows = self._conn.execute(
            "SELECT e.* FROM eventos e WHERE e.status = 'aprovado' "
            "AND (e.titulo_normalizado LIKE ? ESCAPE '\\' "
            "OR e.local_normalizado LIKE ? ESCAPE '\\' "
            "OR lower(e.descricao) LIKE ? ESCAPE '\\') "
            "ORDER BY e.inicio_em ASC LIMIT ?",
            (like_norm, like_norm, like_raw, max(1, limit)),
        ).fetchall()
        hits = [
            e
            for e in (_row_to_event(self._conn, r) for r in rows)
            if needle in normalizar_texto(f"{e.title} {e.venue} {e.description}")
        ]
        return hits

    def count_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM eventos GROUP BY status"
        ):
            counts[_STATUS_DO_CANONICO.get(row["status"], EventStatus.PENDING).value] = int(
                row["n"]
            )
        publicados = int(
            self._conn.execute(
                "SELECT COUNT(DISTINCT t.evento_id) AS n FROM tentativas_publicacao t "
                "JOIN eventos e ON e.id = t.evento_id "
                "WHERE e.status = 'aprovado' AND t.modo = 'execute' AND t.resultado = 'sent'"
            ).fetchone()["n"]
        )
        if publicados:
            restantes = counts.get("approved", 0) - publicados
            if restantes > 0:
                counts["approved"] = restantes
            else:
                counts.pop("approved", None)
            counts["published"] = publicados
        return counts

    def all_for_author(self, telegram_id: int, limit: int = 20) -> list[Event]:
        rows = self._conn.execute(
            "SELECT e.* FROM eventos e WHERE e.enviado_por = ? "
            "ORDER BY e.inicio_em ASC LIMIT ?",
            (telegram_id, max(1, limit)),
        ).fetchall()
        return [_row_to_event(self._conn, r) for r in rows]


class SqliteUserRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_role(self, telegram_id: int) -> UserRole:
        row = self._conn.execute(
            "SELECT papel FROM usuarios WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row is None:
            return UserRole.PUBLIC
        try:
            return UserRole(row["papel"])
        except ValueError:
            return UserRole.PUBLIC

    def upsert(self, telegram_id: int, username: str, role: UserRole) -> None:
        now = now_iso()
        with self._conn:
            # ``username`` vazio não apaga o que já estava gravado: /papel e o
            # bootstrap de ADMIN_IDS chamam upsert sem nome e antes isso
            # apagava o @username do operador.
            self._conn.execute(
                "INSERT INTO usuarios (telegram_id, username, papel, criado_em, atualizado_em) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET "
                "username=CASE WHEN excluded.username <> '' THEN excluded.username "
                "ELSE usuarios.username END, "
                "papel=excluded.papel, "
                "atualizado_em=excluded.atualizado_em",
                (telegram_id, (username or "")[:64], role.value, now, now),
            )

    def list_all(self, limit: int = 50) -> list[tuple[int, str, UserRole]]:
        rows = self._conn.execute(
            "SELECT telegram_id, username, papel FROM usuarios "
            "ORDER BY papel DESC, telegram_id LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        out: list[tuple[int, str, UserRole]] = []
        for r in rows:
            try:
                out.append((r["telegram_id"], r["username"], UserRole(r["papel"])))
            except ValueError:
                out.append((r["telegram_id"], r["username"], UserRole.PUBLIC))
        return out


def _as_int(value: object) -> Optional[int]:
    texto = str(value if value is not None else "").strip()
    return int(texto) if texto.lstrip("-").isdigit() else None


def _escapar_like(texto: str) -> str:
    """Escapa curingas do LIKE (``%``, ``_``) e a barra de escape."""
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Nome de entidade usado pelo bot e pelo núcleo para o mesmo registro.
_ALIAS_ENTIDADE: dict[str, tuple[str, ...]] = {
    "event": ("event", "evento"),
    "evento": ("evento", "event"),
}


class SqliteAuditRepository:
    """Writes to the canonical append-only ``auditoria`` table."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def log(
        self,
        actor_id: Optional[int],
        action: str,
        entity: str,
        entity_id: Optional[str] = None,
        outcome: str = "info",
        detail: str = "",
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO auditoria (entidade, entidade_id, acao, estado_anterior, "
                "estado_novo, ator, detalhe, ocorrido_em) VALUES (?,?,?,?,?,?,?,?)",
                (
                    (entity or "")[:32],
                    _as_int(entity_id),
                    (action or "")[:64],
                    None,
                    (outcome or "info")[:16],
                    str(actor_id) if actor_id else "sistema",
                    (detail or "")[:500],
                    now_iso(),
                ),
            )

    def for_entity(self, entity: str, entity_id: str) -> list[AuditEntry]:
        # O núcleo canônico grava ``entidade='evento'`` (ingestão, aprovação,
        # rejeição) e o bot grava ``'event'``. Ler só um dos nomes escondia
        # justamente a rejeição e a ingestão de /auditoria — a trilha existia,
        # mas o operador não a via.
        nomes = _ALIAS_ENTIDADE.get(entity, (entity,))
        sql = (
            "SELECT * FROM auditoria WHERE entidade IN ("
            + ",".join("?" for _ in nomes)
            + ") AND entidade_id = ? ORDER BY id ASC"
        )
        rows = self._conn.execute(sql, (*nomes, _as_int(entity_id))).fetchall()
        return [_row_to_audit(r) for r in rows]

    def recent(self, limit: int = 50) -> list[AuditEntry]:
        rows = self._conn.execute(
            "SELECT * FROM auditoria ORDER BY id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [_row_to_audit(r) for r in rows]


class SqlitePublishAttemptRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def record(
        self, event_id: int, mode: str, outcome: str, detail: str = "", referencia: str = ""
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO tentativas_publicacao "
                "(evento_id, modo, resultado, detalhe, referencia, criado_em) "
                "VALUES (?,?,?,?,?,?)",
                (event_id, mode, outcome, (detail or "")[:500], referencia or "", now_iso()),
            )

    def list_for_event(self, event_id: int) -> list[PublishAttempt]:
        rows = self._conn.execute(
            "SELECT * FROM tentativas_publicacao WHERE evento_id = ? ORDER BY id ASC",
            (event_id,),
        ).fetchall()
        return [
            PublishAttempt(
                id=r["id"],
                event_id=r["evento_id"],
                mode=r["modo"],
                outcome=r["resultado"],
                detail=r["detalhe"],
                created_at=r["criado_em"],
            )
            for r in rows
        ]

    def last_executed_at(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT criado_em FROM tentativas_publicacao "
            "WHERE modo = 'execute' AND resultado = 'sent' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["criado_em"] if row else None

    def executed_count_since(self, since_iso: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tentativas_publicacao "
            "WHERE modo = 'execute' AND resultado = 'sent' AND criado_em >= ?",
            (since_iso,),
        ).fetchone()
        return int(row["n"])


def _row_to_audit(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        actor_id=_as_int(row["ator"]),
        action=row["acao"],
        entity=row["entidade"],
        entity_id=None if row["entidade_id"] is None else str(row["entidade_id"]),
        outcome=row["estado_novo"] or "info",
        detail=row["detalhe"],
        created_at=row["ocorrido_em"],
    )


# --------------------------------------------------------------------------
# Feiras (somente leitura no bot; carga é feita pela CLI `feiras.cli`)
# --------------------------------------------------------------------------
class FeiraRepository(Protocol):
    def listar(
        self, *, dia: Optional[str] = None, bairro: Optional[str] = None,
        limite: int = 40,
    ) -> list: ...

    def buscar(self, termo: str, limite: int = 10) -> list: ...

    def obter(self, slug_ou_id: str): ...

    def evidencias(self, feira_id: int) -> list: ...

    def resumo(self) -> dict: ...


class SqliteFeiraRepository:
    """Adaptador fino sobre ``feiras.repository`` (mesmo banco e schema).

    O bot nunca escreve em feiras: consulta pública é leitura pura.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def listar(self, *, dia: Optional[str] = None, bairro: Optional[str] = None,
               limite: int = 40) -> list:
        from feiras.repository import listar as _listar

        return _listar(self._conn, dia=dia, bairro=bairro, limite=limite)

    def buscar(self, termo: str, limite: int = 10) -> list:
        from feiras.repository import buscar as _buscar

        return _buscar(self._conn, termo, limite=limite)

    def obter(self, slug_ou_id: str):
        from feiras.repository import obter as _obter

        return _obter(self._conn, slug_ou_id)

    def evidencias(self, feira_id: int) -> list:
        from feiras.repository import evidencias as _evidencias

        return _evidencias(self._conn, feira_id)

    def resumo(self) -> dict:
        from feiras.repository import resumo as _resumo

        return _resumo(self._conn)
