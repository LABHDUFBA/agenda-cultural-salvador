"""Nenhuma credencial no repositório; preflight e gitignore coerentes.

Funciona com ou sem repositório git inicializado: sem git, varre a árvore de
trabalho (excluindo ambientes virtuais, caches e dados locais).
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from conftest import REPO, run

TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{33,}\b")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
             "node_modules", "data", "backups"}
SKIP_SUFFIXES = {".gz", ".db", ".png", ".jpg", ".jpeg", ".pdf", ".zip", ".pyc", ".woff"}


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=60)


def has_git() -> bool:
    return git("rev-parse", "--is-inside-work-tree").returncode == 0


def tracked_files() -> list[str]:
    proc = git("ls-files")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def worktree_files() -> list[str]:
    found: list[str] = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            found.append(os.path.relpath(os.path.join(root, name), REPO))
    return found


def repo_files() -> list[str]:
    return tracked_files() if has_git() else worktree_files()


def test_env_nao_esta_versionado():
    if has_git():
        files = tracked_files()
        assert ".env" not in files
        assert not [f for f in files if f.startswith(".env.") and f != ".env.example"]
    else:
        # Sem git, o risco é o arquivo existir na árvore entregue.
        assert not (REPO / ".env").exists(), ".env presente no diretório do projeto"


def test_env_example_esta_no_projeto():
    assert (REPO / ".env.example").exists()
    if has_git():
        assert ".env.example" in tracked_files()


def test_nenhum_token_em_arquivos_do_projeto():
    suspects = []
    for name in repo_files():
        path = REPO / name
        if not path.is_file() or path.stat().st_size > 512_000:
            continue
        if path.suffix in SKIP_SUFFIXES or path.name == ".env":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if TOKEN_RE.search(text):
            suspects.append(name)
    assert not suspects, f"possível token no projeto: {suspects}"


def test_banco_e_backups_ignorados():
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("data/*.db", ".env", "*.pyc"):
        assert pattern in ignore


def test_preflight_aprovado_sem_token(workdir):
    """Sem BOT_TOKEN o modo dev apenas avisa; nada de erro bloqueante."""
    proc = run(["scripts/preflight.py", "--mode", "dev", "--scan-repo", "--json"],
               env={"AGENDA_DB": str(workdir / "data" / "agenda.db"),
                    "AGENDA_BACKUP_DIR": str(workdir / "backups")})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_preflight_prod_exige_credenciais(workdir):
    proc = run(["scripts/preflight.py", "--mode", "prod", "--json"],
               env={"BOT_TOKEN": "", "TARGET_CHAT_ID": "", "ADMIN_IDS": "",
                    "AGENDA_DB": str(workdir / "data" / "agenda.db"),
                    "AGENDA_BACKUP_DIR": str(workdir / "backups")})
    assert proc.returncode == 1
    assert "BOT_TOKEN" in proc.stdout


def test_preflight_detecta_token_malformado(workdir):
    proc = run(["scripts/preflight.py", "--mode", "prod", "--json"],
               env={"BOT_TOKEN": "abc123", "TARGET_CHAT_ID": "-1001234567890",
                    "ADMIN_IDS": "1",
                    "AGENDA_DB": str(workdir / "data" / "agenda.db"),
                    "AGENDA_BACKUP_DIR": str(workdir / "backups")})
    assert proc.returncode == 1
    assert "formato" in proc.stdout


def test_preflight_nao_imprime_segredo(workdir):
    """Token válido no formato não pode aparecer na saída, nem truncado."""
    # Montado em pedaços: o literal completo nunca existe no arquivo, senão a
    # própria varredura de segredos acusaria este teste.
    token = ":".join(["123456789", "AAH" + "fake_" * 6 + "fake1"])
    proc = run(["scripts/preflight.py", "--mode", "prod", "--json"],
               env={"BOT_TOKEN": token, "TARGET_CHAT_ID": "-1001234567890", "ADMIN_IDS": "1",
                    "AGENDA_DB": str(workdir / "data" / "agenda.db"),
                    "AGENDA_BACKUP_DIR": str(workdir / "backups")})
    assert token not in proc.stdout
    assert token.split(":")[1][:8] not in proc.stdout
    assert proc.returncode == 0, proc.stdout


def test_preflight_reporta_janelas_de_publicacao_bloqueadas(workdir):
    proc = run(["scripts/preflight.py", "--mode", "dev", "--json"],
               env={"AGENDA_DB": str(workdir / "data" / "agenda.db"),
                    "AGENDA_BACKUP_DIR": str(workdir / "backups")})
    assert proc.returncode == 0, proc.stdout
    data = json.loads(proc.stdout)
    codes = {item["code"] for item in data["items"]}
    assert "schedule.publish_blocked" in codes
    assert "schedule.invalid" not in codes


def test_preflight_avisa_backup_ausente(workdir):
    (workdir / "backups").mkdir()
    proc = run(["scripts/preflight.py", "--mode", "dev", "--json"],
               env={"AGENDA_DB": str(workdir / "data" / "agenda.db"),
                    "AGENDA_BACKUP_DIR": str(workdir / "backups")})
    assert proc.returncode == 0, proc.stdout
    data = json.loads(proc.stdout)
    assert any(item["code"] == "backup.empty" for item in data["items"])


def test_compose_nao_declara_credencial():
    """Credencial vem do .env; no compose só referência `${VAR:-}`, nunca valor."""
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert not TOKEN_RE.search(text)
    assert "BOT_TOKEN=" not in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("BOT_TOKEN:"):
            # Referência de interpolação é aceita (valor vazio por padrão);
            # qualquer outra forma seria credencial embutida.
            assert stripped == "BOT_TOKEN: ${BOT_TOKEN:-}", (
                f"compose declara credencial inline: {stripped}"
            )
