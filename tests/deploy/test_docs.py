"""Documentação de implantação: existe, é acionável e não promete o que não há."""

from __future__ import annotations

import re

import pytest

from conftest import REPO

DOCS = ["deploy.md", "operations.md", "security.md", "scheduling.md", "ia.md"]


@pytest.mark.parametrize("name", DOCS)
def test_documento_existe_e_tem_conteudo(name):
    path = REPO / "docs" / name
    assert path.exists(), f"falta docs/{name}"
    text = path.read_text(encoding="utf-8")
    assert len(text) > 1200, f"docs/{name} raso demais"
    assert text.lstrip().startswith("#"), "documento sem título"
    # "Todo" (português) é palavra legítima; só marcador de pendência é falha.
    assert not re.search(r"\b(TODO|FIXME)\b", text), f"docs/{name} tem marcador de pendência"


def test_readme_aponta_para_implantacao():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "docs/deploy.md" in readme
    assert "docker compose" in readme
    assert "America/Bahia" in readme


def test_deploy_doc_cobre_comandos_essenciais():
    text = (REPO / "docs" / "deploy.md").read_text(encoding="utf-8")
    for snippet in ("cp .env.example .env", "docker compose config",
                    "docker compose up -d", "scripts/healthcheck.py",
                    "scripts/backup.py"):
        assert snippet in text, f"docs/deploy.md sem {snippet!r}"


def test_operations_doc_cobre_restauracao_e_backup():
    text = (REPO / "docs" / "operations.md").read_text(encoding="utf-8")
    for snippet in ("RESTAURAR", "backup.py verify", "deploy_check.sh", "heartbeat"):
        assert snippet in text


def test_security_doc_nao_pede_credencial_no_repo():
    text = (REPO / "docs" / "security.md").read_text(encoding="utf-8")
    assert ".env" in text
    assert "AGENDA_ALLOW_EXECUTE" in text
    assert "dry-run" in text.lower()


def test_scheduling_doc_cobre_dias_e_janelas_bloqueadas():
    text = (REPO / "docs" / "scheduling.md").read_text(encoding="utf-8")
    for snippet in ("`days`", "quinta", "06:00", "22:00",
                    "DECLARABLE_TASKS", "schedule_digest", "--only-due"):
        assert snippet in text, f"docs/scheduling.md sem {snippet!r}"
