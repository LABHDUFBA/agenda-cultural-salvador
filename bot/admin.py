"""Offline administration CLI (no network, no Telegram).

Usage:
    python -m bot.admin init --admin 12345
    python -m bot.admin admin add 12345 carol
    python -m bot.admin admin list
    python -m bot.admin events --status pending_review
    python -m bot.admin event 7
    python -m bot.admin authorize 7 "e-mail da produtora, 01/11/2026"
    python -m bot.admin approve 7 --actor 12345
    python -m bot.admin publish 7 --actor 12345 [--execute --confirm PUBLICAR]
    python -m bot.admin audit 7
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .config import Settings
from .models import Event, EventStatus, UserRole
from .runtime import build_runtime


def _settings(args) -> Settings:
    settings = Settings.from_env()
    if args.db:
        settings = Settings(**{**settings.__dict__, "db_path": args.db})
    return settings


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="bot.admin", description=__doc__)
    parser.add_argument("--db", help="caminho do SQLite (padrão: AGENDA_DB ou data/agenda.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="cria esquema e promove admins iniciais")
    p_init.add_argument("--admin", type=int, action="append", default=[])

    p_admin = sub.add_parser("admin", help="gerencia perfis")
    p_admin.add_argument("action", choices=["add", "list"])
    p_admin.add_argument("telegram_id", nargs="?", type=int)
    p_admin.add_argument("username", nargs="?", default="")
    p_admin.add_argument("--role", default="admin", choices=[r.value for r in UserRole])

    p_events = sub.add_parser("events", help="lista eventos")
    p_events.add_argument("--status", action="append", default=[])

    p_event = sub.add_parser("event", help="mostra um evento e pendências")
    p_event.add_argument("id", type=int)

    p_auth = sub.add_parser("authorize", help="registra autorização do organizador")
    p_auth.add_argument("id", type=int)
    p_auth.add_argument("reference")
    p_auth.add_argument("--actor", type=int, default=0)

    p_appr = sub.add_parser("approve", help="aprova evento")
    p_appr.add_argument("id", type=int)
    p_appr.add_argument("--actor", type=int, default=0)

    p_pub = sub.add_parser("publish", help="publica (dry-run por padrão)")
    p_pub.add_argument("id", type=int)
    p_pub.add_argument("--actor", type=int, required=True)
    p_pub.add_argument("--execute", action="store_true")
    p_pub.add_argument("--confirm", default="")

    p_audit = sub.add_parser("audit", help="histórico de auditoria")
    p_audit.add_argument("id", type=int)

    args = parser.parse_args(argv)
    settings = _settings(args)
    runtime = build_runtime(settings)
    ctx = runtime.context

    if args.cmd == "init":
        for admin_id in args.admin:
            ctx.users.upsert(admin_id, "", UserRole.ADMIN)
        print(f"esquema pronto em {settings.db_path}; admins: {args.admin or 'nenhum'}")
        return 0

    if args.cmd == "admin":
        if args.action == "add":
            if not args.telegram_id:
                print("informe o telegram_id", file=sys.stderr)
                return 2
            ctx.users.upsert(args.telegram_id, args.username, UserRole(args.role))
            print(f"{args.telegram_id} -> {args.role}")
        else:
            for tid, username, role in ctx.users.list_all():
                print(json.dumps({"id": tid, "username": username, "role": role.value}))
        return 0

    if args.cmd == "events":
        statuses = [EventStatus(s) for s in args.status] or list(EventStatus)
        for e in ctx.events.list_by_status(statuses, limit=100):
            print(json.dumps({
                "id": e.id, "title": e.title, "starts_at": e.starts_at,
                "status": e.status.value, "authorized": e.authorization_confirmed,
            }, ensure_ascii=False))
        return 0

    if args.cmd == "event":
        event = ctx.events.get(args.id)
        gaps = event.authorization_gaps()
        print(json.dumps({
            "id": event.id, "title": event.title, "venue": event.venue,
            "starts_at": event.starts_at, "status": event.status.value,
            "authorization_confirmed": event.authorization_confirmed,
            "authorization_reference": event.authorization_reference,
            "source_url": event.source_url,
            "published_at": event.published_at,
            "publish_reference": event.publish_reference,
            "blockers": gaps,
        }, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "authorize":
        event = ctx.events.get(args.id)
        updated = Event(**{**event.__dict__,
                           "authorization_confirmed": True,
                           "authorization_reference": args.reference})
        ctx.events.update(updated)
        ctx.audit.log(args.actor or None, "event.authorize", "event", str(args.id),
                      "allowed", f"cli ref={args.reference}")
        print(f"#{args.id} autorizado")
        return 0

    if args.cmd == "approve":
        event = ctx.events.get(args.id)
        if not event.authorization_confirmed:
            print("bloqueado: autorização do organizador não confirmada", file=sys.stderr)
            return 1
        ctx.events.update(Event(**{**event.__dict__, "status": EventStatus.APPROVED}))
        ctx.audit.log(args.actor or None, "event.approve", "event", str(args.id), "allowed", "cli")
        print(f"#{args.id} aprovado")
        return 0

    if args.cmd == "publish":
        execute = bool(args.execute)
        confirm = args.confirm if execute else None
        result = runtime.publisher.publish(
            args.id, args.actor, dry_run=not execute, confirmation=confirm
        )
        print(json.dumps({
            "event_id": result.event_id, "outcome": result.outcome,
            "message_id": result.message_id, "detail": result.detail,
            "preview": result.preview,
        }, ensure_ascii=False, indent=2))
        return 0 if result.outcome in ("dry_run", "sent", "skipped") else 1

    if args.cmd == "audit":
        for entry in ctx.audit.for_entity("event", str(args.id)):
            print(json.dumps({
                "at": entry.created_at, "actor": entry.actor_id, "action": entry.action,
                "outcome": entry.outcome, "detail": entry.detail,
            }, ensure_ascii=False))
        return 0

    print(f"comando não implementado: {args.cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
