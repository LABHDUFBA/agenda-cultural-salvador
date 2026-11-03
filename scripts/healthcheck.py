#!/usr/bin/env python3
"""Healthcheck de implantação — sem rede, sem token, sem escrita.

Usado como HEALTHCHECK do container e em runbooks.  Sai com:

    0  saudável
    1  degradado (banco ausente, corrompido, schema antigo ou heartbeat velho)
    2  erro de uso/argumento

Exemplos:
    python scripts/healthcheck.py
    python scripts/healthcheck.py --quiet
    python scripts/healthcheck.py --json
    python scripts/healthcheck.py --scheduler          # agendador vivo?
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = os.environ.get("AGENDA_DB") or "data/agenda.db"
DEFAULT_HEARTBEAT = os.environ.get("AGENDA_SCHEDULER_STATE") or "data/scheduler_state.json"
# O agendador grava heartbeat a cada tick (padrão 30 s); 15 min é folga segura.
DEFAULT_MAX_AGE = 900


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def check_db(path: Path) -> dict:
    """Checagem somente leitura.  Nunca cria o arquivo se ele não existir."""
    if not path.exists():
        return {"ok": False, "reason": "banco ausente", "db": str(path)}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return {"ok": False, "reason": f"abertura falhou: {exc}", "db": str(path)}

    try:
        conn.execute("PRAGMA busy_timeout = 3000")
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # Esquema único (database/schema.sql): eventos + tabelas operacionais.
        # "events" existe apenas em bancos legados do bot antigo; continua
        # sendo contado para não esconder dados de uma instalação anterior.
        table_name = "eventos" if "eventos" in tables else ("events" if "events" in tables else None)
        events = None
        if table_name:
            try:
                events = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            except sqlite3.Error:
                events = None
        schema_ok = version >= 1 or table_name is not None
        info = {
            "ok": check == "ok" and schema_ok,
            "db": str(path),
            "integrity": check,
            "schema_version": version,
            "tables": len(tables),
            "event_table": table_name,
            "events": events,
            "size_bytes": path.stat().st_size,
        }
        if check != "ok":
            info["reason"] = f"integridade: {check}"
        elif not schema_ok:
            info["reason"] = "schema não inicializado"
        return info
    except sqlite3.Error as exc:
        return {"ok": False, "db": str(path), "reason": f"consulta falhou: {exc}"}
    finally:
        conn.close()


def check_heartbeat(path: Path, max_age: int, schedule_path: Path | None = None) -> dict:
    if not path.exists():
        return {"ok": False, "reason": "heartbeat ausente", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data["heartbeat_at"]
        beat = datetime.fromisoformat(raw)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "reason": f"heartbeat ilegível: {exc}", "path": str(path)}
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - beat).total_seconds()
    ok = 0 <= age <= max_age
    result = {
        "ok": ok,
        "path": str(path),
        "age_seconds": int(age),
        "max_age_seconds": max_age,
        "last_status": data.get("last_status"),
        "schedule_digest": data.get("schedule_digest"),
        "reason": None if ok else f"heartbeat com {int(age)}s (> {max_age}s)",
    }

    # Deriva de configuração: o processo em execução pode estar com uma agenda
    # diferente da que está no disco (editada sem restart).
    if schedule_path is not None:
        expected = _file_digest(schedule_path)
        result["expected_schedule_digest"] = expected
        if expected is None:
            result["ok"] = False
            result["reason"] = f"agenda ilegível: {schedule_path}"
        elif result["schedule_digest"] and expected != result["schedule_digest"]:
            result["ok"] = False
            result["reason"] = "agenda em disco diferente da carregada pelo agendador (restart necessário)"
    return result


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="healthcheck", description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="caminho do SQLite")
    parser.add_argument("--scheduler", action="store_true",
                        help="verifica o heartbeat do agendador em vez do banco")
    parser.add_argument("--state", default=DEFAULT_HEARTBEAT, help="arquivo de estado do agendador")
    parser.add_argument("--schedule", default=None,
                        help="com --scheduler: compara o digest da agenda em disco com o do heartbeat")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                        help="idade máxima do heartbeat em segundos (padrão 900)")
    parser.add_argument("--json", action="store_true", help="saída JSON")
    parser.add_argument("--quiet", action="store_true", help="só o código de saída")
    args = parser.parse_args(argv)

    if args.scheduler:
        result = check_heartbeat(_resolve(args.state), args.max_age,
                                 _resolve(args.schedule) if args.schedule else None)
    else:
        result = check_db(_resolve(args.db))

    if not args.quiet:
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            label = "OK" if result["ok"] else "FALHA"
            detail = result.get("reason") or (
                f"eventos={result.get('events')} schema={result.get('schema_version')}"
                if "events" in result else "vivo"
            )
            print(f"[{label}] {detail} ({result.get('db') or result.get('path')})")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
