"""Healthcheck: banco saudável, ausente, sem schema, corrompido; heartbeat."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from conftest import run, run_json


def test_banco_do_bot_e_saudavel(db):
    data, proc = run_json(["scripts/healthcheck.py", "--db", str(db), "--json"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert data["ok"] is True
    assert data["integrity"] == "ok"
    assert data["event_table"] == "eventos"


def test_quiet_nao_imprime_nada_quando_ok(db):
    proc = run(["scripts/healthcheck.py", "--db", str(db), "--quiet"])
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_banco_ausente_falha(tmp_path):
    data, proc = run_json(["scripts/healthcheck.py", "--db", str(tmp_path / "nada.db"), "--json"])
    assert proc.returncode == 1
    assert data["ok"] is False
    assert "ausente" in data["reason"]


def test_arquivo_sem_schema_falha(tmp_path):
    empty = tmp_path / "vazio.db"
    conn = sqlite3.connect(str(empty))
    conn.execute("CREATE TABLE outra (id INTEGER)")
    conn.commit()
    conn.close()
    data, proc = run_json(["scripts/healthcheck.py", "--db", str(empty), "--json"])
    assert proc.returncode == 1
    assert data["ok"] is False


def test_arquivo_corrompido_falha(tmp_path):
    broken = tmp_path / "quebrado.db"
    broken.write_bytes(b"isto nao e um banco sqlite" * 40)
    data, proc = run_json(["scripts/healthcheck.py", "--db", str(broken), "--json"])
    assert proc.returncode == 1
    assert data["ok"] is False


def _heartbeat(path, when: datetime) -> None:
    path.write_text(json.dumps({
        "heartbeat_at": when.isoformat(timespec="seconds"),
        "pid": 1,
        "last_status": 0,
    }), encoding="utf-8")


def test_heartbeat_recente_e_ok(tmp_path):
    state = tmp_path / "state.json"
    _heartbeat(state, datetime.now(timezone.utc) - timedelta(seconds=30))
    data, proc = run_json(["scripts/healthcheck.py", "--scheduler", "--state", str(state), "--json"])
    assert proc.returncode == 0
    assert data["ok"] is True
    assert data["age_seconds"] < 120


def test_heartbeat_velho_falha(tmp_path):
    state = tmp_path / "state.json"
    _heartbeat(state, datetime.now(timezone.utc) - timedelta(hours=3))
    data, proc = run_json(["scripts/healthcheck.py", "--scheduler", "--state", str(state), "--json"])
    assert proc.returncode == 1
    assert data["ok"] is False


def test_heartbeat_ausente_falha(tmp_path):
    proc = run(["scripts/healthcheck.py", "--scheduler", "--state", str(tmp_path / "x.json")])
    assert proc.returncode == 1


def test_heartbeat_com_digest_divergente_falha(tmp_path):
    """Agenda editada sem restart: o processo em memória está desatualizado."""
    state = tmp_path / "state.json"
    schedule = tmp_path / "schedule.json"
    schedule.write_text('{"jobs": []}', encoding="utf-8")
    state.write_text(json.dumps({
        "heartbeat_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": 1,
        "last_status": 0,
        "schedule_digest": "0" * 64,
    }), encoding="utf-8")
    data, proc = run_json(["scripts/healthcheck.py", "--scheduler", "--state", str(state),
                           "--schedule", str(schedule), "--json"])
    assert proc.returncode == 1
    assert "restart" in data["reason"]


def test_heartbeat_com_digest_igual_e_ok(tmp_path):
    import hashlib

    state = tmp_path / "state.json"
    schedule = tmp_path / "schedule.json"
    schedule.write_text('{"jobs": []}', encoding="utf-8")
    digest = hashlib.sha256(schedule.read_bytes()).hexdigest()
    state.write_text(json.dumps({
        "heartbeat_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": 1,
        "last_status": 0,
        "schedule_digest": digest,
    }), encoding="utf-8")
    data, proc = run_json(["scripts/healthcheck.py", "--scheduler", "--state", str(state),
                           "--schedule", str(schedule), "--json"])
    assert proc.returncode == 0, data
    assert data["ok"] is True
