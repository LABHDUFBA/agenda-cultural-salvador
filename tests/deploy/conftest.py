"""Fixtures dos testes de implantação.

Estes testes não tocam a rede e não dependem do daemon Docker: eles validam
os artefatos (Dockerfile, compose, .env.example, scripts) e o comportamento
dos scripts de operação em diretórios temporários.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def pytest_configure(config):
    config.addinivalue_line("markers", "deploy: testes de implantação (artefatos e scripts)")


def pytest_collection_modifyitems(config, items):
    marker = pytest.mark.deploy
    for item in items:
        if "tests/deploy" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(marker)


def run(args: list[str], env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """Executa um comando do repositório com cwd na raiz.

    O ambiente do teste é hermético: variáveis ``AGENDA_*`` herdadas da sessão
    do desenvolvedor/CI são removidas antes de aplicar ``env``. Sem isso um
    ``AGENDA_LOG_LEVEL=WARNING`` no shell mudava a saída observada e o mesmo
    teste passava ou falhava conforme quem o executasse.
    """
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("AGENDA_")}
    full_env.pop("BOT_TOKEN", None)
    full_env.setdefault("AGENDA_ALLOW_EXECUTE", "0")
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )


def run_json(args: list[str], env: dict | None = None) -> tuple[dict, subprocess.CompletedProcess]:
    proc = run(args, env=env)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - falha de diagnóstico
        raise AssertionError(f"saída não é JSON: {proc.stdout!r} / {proc.stderr!r}") from exc
    return data, proc


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    return tmp_path


@pytest.fixture()
def db(workdir: Path) -> Path:
    """Banco com o schema do bot (events/users/audit_log/publish_attempts)."""
    path = workdir / "data" / "agenda.db"
    proc = run(["-m", "bot.admin", "--db", str(path), "init", "--admin", "123456789"])
    assert proc.returncode == 0, proc.stderr
    assert path.exists()
    return path


@pytest.fixture()
def env_for(workdir: Path):
    def _make(db_path: Path, backup_dir: Path | None = None, **extra: str) -> dict:
        base = {
            "AGENDA_DB": str(db_path),
            "AGENDA_BACKUP_DIR": str(backup_dir or (workdir / "backups")),
            "AGENDA_SCHEDULER_STATE": str(workdir / "state.json"),
            "AGENDA_SCHEDULE": str(REPO / "config" / "schedule.json"),
        }
        base.update(extra)
        return base

    return _make
