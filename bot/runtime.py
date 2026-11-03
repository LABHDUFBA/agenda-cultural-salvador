"""Wiring: build the concrete SQLite runtime.  No network is touched here."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Settings
from .db import (
    SqliteAuditRepository,
    SqliteEventRepository,
    SqliteFeiraRepository,
    SqlitePublishAttemptRepository,
    SqliteUserRepository,
    connect,
    init_schema,
)
from .handlers import BotContext, BotHandlers, Throttle
from .models import UserRole
from .permissions import PermissionService
from .publisher import Publisher
from .telegram_api import NullTransport, Transport


@dataclass
class Runtime:
    conn: sqlite3.Connection
    context: BotContext
    handlers: BotHandlers
    transport: Transport
    publisher: Publisher


def build_runtime(settings: Settings, transport: Transport | None = None) -> Runtime:
    conn = connect(settings.db_path)
    init_schema(conn)

    events = SqliteEventRepository(conn)
    users = SqliteUserRepository(conn)
    audit = SqliteAuditRepository(conn)
    attempts = SqlitePublishAttemptRepository(conn)
    feiras = SqliteFeiraRepository(conn)

    for admin_id in settings.admin_ids:
        if users.get_role(admin_id) is not UserRole.ADMIN:
            users.upsert(admin_id, "", UserRole.ADMIN)

    permissions = PermissionService(users, audit)
    real_transport = transport or NullTransport()
    publisher = Publisher(
        events, attempts, audit, permissions, real_transport, settings.publisher_config()
    )
    context = BotContext(
        events, users, audit, permissions, publisher, Throttle(), feiras
    )
    return Runtime(conn, context, BotHandlers(context), real_transport, publisher)
