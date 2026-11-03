"""Backup, verificação, retenção e restauração — em diretórios temporários."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from conftest import run, run_json


def test_create_gera_arquivo_e_checksum(db, workdir, env_for):
    backups = workdir / "backups"
    data, proc = run_json(["scripts/backup.py", "create", "--stamp", "20260101T000000Z"],
                          env=env_for(db, backups))
    assert proc.returncode == 0, proc.stderr
    archive = Path(data["created"])
    assert archive.exists() and archive.name == "agenda-20260101T000000Z.db.gz"
    sidecar = archive.with_name(archive.name + ".sha256")
    assert sidecar.exists()
    assert data["sha256"] in sidecar.read_text(encoding="ascii")
    assert data["verified"] is True
    assert data["events"] == 0


def test_arquivo_e_gzip_valido(db, workdir, env_for):
    backups = workdir / "backups"
    data, _ = run_json(["scripts/backup.py", "create", "--stamp", "20260101T000001Z"],
                       env=env_for(db, backups))
    with gzip.open(data["created"], "rb") as fh:
        header = fh.read(16)
    assert header.startswith(b"SQLite format 3\x00"), "snapshot não é um SQLite"


def test_verify_aceita_backup_bom(db, workdir, env_for):
    backups = workdir / "backups"
    data, _ = run_json(["scripts/backup.py", "create", "--stamp", "20260101T000002Z"],
                       env=env_for(db, backups))
    result, proc = run_json(["scripts/backup.py", "verify", data["created"]])
    assert proc.returncode == 0, proc.stderr
    assert result["ok"] is True
    assert result["checksum_ok"] is True


def test_verify_rejeita_arquivo_adulterado(db, workdir, env_for):
    backups = workdir / "backups"
    data, _ = run_json(["scripts/backup.py", "create", "--stamp", "20260101T000003Z"],
                       env=env_for(db, backups))
    archive = Path(data["created"])
    archive.write_bytes(archive.read_bytes() + b"lixo")
    result, proc = run_json(["scripts/backup.py", "verify", str(archive)])
    assert proc.returncode == 1
    assert result["ok"] is False


def test_retention_poda_os_antigos(db, workdir, env_for):
    backups = workdir / "backups"
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        run_json(["scripts/backup.py", "create", "--stamp", stamp, "--keep", "10"],
                 env=env_for(db, backups))
    assert len(list(backups.glob("agenda-*.db.gz"))) == 3
    result, proc = run_json(["scripts/backup.py", "prune", "--keep", "2"],
                            env=env_for(db, backups))
    assert proc.returncode == 0, proc.stderr
    remaining = sorted(p.name for p in backups.glob("agenda-*.db.gz"))
    assert remaining == ["agenda-20260102T000000Z.db.gz", "agenda-20260103T000000Z.db.gz"]
    assert result["removed"] == ["agenda-20260101T000000Z.db.gz"]
    # sidecar do arquivo removido também sai
    assert not (backups / "agenda-20260101T000000Z.db.gz.sha256").exists()


def test_dry_run_nao_escreve_nada(db, workdir, env_for):
    backups = workdir / "backups"
    data, proc = run_json(["scripts/backup.py", "create", "--dry-run", "--stamp", "20260101T000004Z"],
                          env=env_for(db, backups))
    assert proc.returncode == 0
    assert data["dry_run"] is True
    assert not backups.exists() or not list(backups.glob("agenda-*.db.gz"))


def test_banco_ausente_falha_com_codigo_2(workdir, env_for):
    proc = run(["scripts/backup.py", "create"],
               env=env_for(workdir / "nao-existe.db"))
    assert proc.returncode == 2
    assert "não encontrado" in proc.stderr


def test_restore_exige_frase_de_confirmacao(db, workdir, env_for):
    backups = workdir / "backups"
    data, _ = run_json(["scripts/backup.py", "create", "--stamp", "20260101T000005Z"],
                       env=env_for(db, backups))
    target = workdir / "restored.db"
    proc = run(["scripts/backup.py", "restore", "--file", data["created"], "--to", str(target)])
    assert proc.returncode == 2
    assert "RESTAURAR" in proc.stderr
    assert not target.exists()


def test_restore_nao_sobrescreve_sem_force(db, workdir, env_for):
    backups = workdir / "backups"
    data, _ = run_json(["scripts/backup.py", "create", "--stamp", "20260101T000006Z"],
                       env=env_for(db, backups))
    target = workdir / "restored.db"
    target.write_bytes(b"existente")
    proc = run(["scripts/backup.py", "restore", "--file", data["created"],
                "--to", str(target), "--confirm", "RESTAURAR"])
    assert proc.returncode == 2
    assert target.read_bytes() == b"existente"


def test_restore_completa_banco_valido(db, workdir, env_for):
    backups = workdir / "backups"
    data, _ = run_json(["scripts/backup.py", "create", "--stamp", "20260101T000007Z"],
                       env=env_for(db, backups))
    target = workdir / "restored.db"
    result, proc = run_json(["scripts/backup.py", "restore", "--file", data["created"],
                             "--to", str(target), "--confirm", "RESTAURAR"])
    assert proc.returncode == 0, proc.stderr
    assert result["events"] == 0
    # banco restaurado é utilizável pelo próprio projeto
    check, check_proc = run_json(["scripts/healthcheck.py", "--db", str(target), "--json"])
    assert check_proc.returncode == 0, check_proc.stdout
    assert check["ok"] is True
    assert target.stat().st_mode & 0o077 == 0, "banco restaurado deveria ser 0600"


def test_list_reflete_arquivos(db, workdir, env_for):
    backups = workdir / "backups"
    run_json(["scripts/backup.py", "create", "--stamp", "20260101T000008Z"],
             env=env_for(db, backups))
    data, proc = run_json(["scripts/backup.py", "list"], env=env_for(db, backups))
    assert proc.returncode == 0
    assert data["count"] == 1
    assert data["items"][0]["checksum"] is True
    json.dumps(data)  # serializável
