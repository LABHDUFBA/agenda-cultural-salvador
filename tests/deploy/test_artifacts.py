"""Testes dos artefatos de imagem: Dockerfile, .dockerignore, compose."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from conftest import REPO  # noqa: E402  (pytest injeta o diretório do teste)

TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{33,}\b")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (REPO / "Dockerfile").read_text(encoding="utf-8")


def test_compose_tem_servicos_esperados(compose):
    services = compose["services"]
    assert {"bot", "scheduler"} <= set(services)
    assert services["bot"]["command"] == ["python", "-m", "bot.app"]
    assert "scheduler.py" in " ".join(services["scheduler"]["command"])


def test_compose_nao_publica_porta(compose):
    # Long-polling: qualquer porta publicada seria exposição desnecessária.
    for name, service in compose["services"].items():
        assert "ports" not in service, f"{name} publica porta"
        assert "expose" not in service, f"{name} expõe porta"


def test_compose_volumes_persistentes(compose):
    for name in ("bot", "scheduler"):
        volumes = compose["services"][name]["volumes"]
        assert any("agenda-data:/app/data" in v for v in volumes)
        assert any("agenda-backups:/app/backups" in v for v in volumes)
    assert {"agenda-data", "agenda-backups"} <= set(compose["volumes"])


def test_compose_fuso_e_caminhos_absolutos(compose):
    for name in ("bot", "scheduler"):
        env = compose["services"][name]["environment"]
        assert "America/Bahia" in env["TZ"]
        assert env["AGENDA_DB"] == "/app/data/agenda.db"
        assert env["AGENDA_BACKUP_DIR"] == "/app/backups"


def test_compose_endurecimento(compose):
    for name in ("bot", "scheduler"):
        service = compose["services"][name]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert "ALL" in service["cap_drop"]
        assert service["read_only"] is True
        assert service.get("restart") == "unless-stopped"
        assert "healthcheck" in service


def test_compose_servicos_de_ferramenta_nao_sobem_por_padrao(compose):
    for name in ("admin", "backup"):
        assert compose["services"][name]["profiles"] == ["tools"]


def test_compose_sem_credencial_literal(compose):
    raw = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert not TOKEN_RE.search(raw)
    assert "BOT_TOKEN=" not in raw          # credencial só via env_file .env
    assert "1234567890:" not in raw


def test_compose_config_valida():
    """`docker compose config` sem daemon; usa uma cópia com .env de modelo."""
    if not shutil.which("docker"):
        pytest.skip("docker CLI ausente")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for name in ("docker-compose.yml", "Dockerfile", ".dockerignore", "requirements.txt"):
            shutil.copy(REPO / name, tmpdir / name)
        shutil.copy(REPO / ".env.example", tmpdir / ".env")
        proc = subprocess.run(["docker", "compose", "config", "--quiet"],
                              cwd=str(tmpdir), capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr


def test_compose_config_valida_sem_env():
    """Sem `.env` o compose continua válido e resolve para o modo seguro.

    Isso é o que permite `docker compose config` em máquina limpa/CI e o que
    garante que a ausência de credencial não quebra a sintaxe.
    """
    if not shutil.which("docker"):
        pytest.skip("docker CLI ausente")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for name in ("docker-compose.yml", "Dockerfile", ".dockerignore", "requirements.txt"):
            shutil.copy(REPO / name, tmpdir / name)
        assert not (tmpdir / ".env").exists()
        proc = subprocess.run(["docker", "compose", "config"],
                              cwd=str(tmpdir), capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        # Modo seguro: sem token, execução de publicação desligada.
        assert 'BOT_TOKEN: ""' in proc.stdout
        assert 'TARGET_CHAT_ID: ""' in proc.stdout
        assert 'AGENDA_ALLOW_EXECUTE: "0"' in proc.stdout
        assert "America/Bahia" in proc.stdout


def test_compose_env_file_e_opcional(compose):
    """`.env` é opcional no compose; o arquivo não pode ser obrigatório."""
    for name in ("bot", "scheduler"):
        env_file = compose["services"][name]["env_file"]
        assert env_file == [{"path": ".env", "required": False}], name


def test_dockerfile_nao_roda_como_root(dockerfile):
    assert re.search(r"^USER\s+\d+", dockerfile, re.MULTILINE), "imagem roda como root"
    assert "useradd" in dockerfile and "--system" in dockerfile


def test_dockerfile_base_e_dependencias(dockerfile):
    assert dockerfile.startswith("# syntax")
    from_line = re.search(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE)
    assert from_line and from_line.group(1) == "python:3.12-slim"
    assert "pip install --no-cache-dir -r requirements.txt" in dockerfile


def test_dockerfile_fuso_e_healthcheck(dockerfile):
    assert "TZ=America/Bahia" in dockerfile
    assert "tzdata" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "scripts/healthcheck.py" in dockerfile


def test_dockerfile_nao_copia_segredo(dockerfile):
    copies = [ln for ln in dockerfile.splitlines()
              if ln.strip().upper().startswith(("COPY ", "ADD "))]
    for line in copies:
        assert ".env" not in line, f"cópia de segredo no build: {line.strip()}"
    assert not TOKEN_RE.search(dockerfile)


def test_dockerignore_exclui_estado_e_segredo():
    text = (REPO / ".dockerignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines()}
    for expected in (".env", "*.db", "backups/", ".git", ".venv"):
        assert expected in lines, f".dockerignore sem {expected}"
    assert "!.env.example" in lines


def test_requirements_runtime_so_stdlib():
    text = (REPO / "requirements.txt").read_text(encoding="utf-8")
    packages = [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    assert packages == [], "runtime deve continuar sem dependências externas"


def test_requirements_dev_fixado():
    text = (REPO / "requirements-dev.txt").read_text(encoding="utf-8")
    pinned = [ln.strip() for ln in text.splitlines()
              if ln.strip() and not ln.strip().startswith(("#", "-r"))]
    assert pinned, "requirements-dev.txt sem dependências"
    for line in pinned:
        assert "==" in line, f"dependência não fixada: {line}"
