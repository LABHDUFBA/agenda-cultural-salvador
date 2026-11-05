"""O modelo .env.example precisa cobrir tudo que o código lê — e nada secreto."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import REPO

TOKEN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{33,}\b")
VAR_READ_RE = re.compile(
    r'(?:os\.environ\.get|env\.get|_flag|_int)\(\s*"([A-Z][A-Z0-9_]+)"'
)


def parse_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def code_referenced_vars() -> set[str]:
    names: set[str] = set()
    for path in list((REPO / "bot").rglob("*.py")) + list((REPO / "scripts").glob("*.py")):
        names.update(VAR_READ_RE.findall(path.read_text(encoding="utf-8")))
    return names


def test_env_example_existe_e_e_parseavel():
    values = parse_env_example()
    assert len(values) >= 10, values


def test_env_example_cobre_variaveis_do_codigo():
    values = parse_env_example()
    missing = sorted(v for v in code_referenced_vars() if v not in values)
    assert not missing, f"variáveis lidas pelo código e ausentes do modelo: {missing}"


def test_env_example_tem_fuso_de_salvador():
    assert parse_env_example()["TZ"] == "America/Bahia"


def test_env_example_nao_tem_credencial():
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    assert not TOKEN_RE.search(text)
    values = parse_env_example()
    for key in ("BOT_TOKEN", "TARGET_CHAT_ID", "ADMIN_IDS"):
        assert values[key] == "", f"{key} deveria estar vazio no modelo"


def test_env_example_padroes_seguros():
    values = parse_env_example()
    assert values["AGENDA_ALLOW_EXECUTE"] == "0", "modelo não pode liberar envio real"
    assert values["AGENDA_SCHEDULER_DRY_RUN"] == "0"
    assert int(values["AGENDA_BACKUP_KEEP"]) >= 7


def test_env_example_documenta_ia_opcional_desligada():
    """A IA é opcional: o modelo precisa vir desligada e sem chave."""
    values = parse_env_example()
    assert values["AGENDA_IA_ENABLED"] == "0"
    assert values["REFUSELESS_API_KEY"] == ""
    assert values["AGENDA_IA_BASE_URL"] == "https://api.refuseless.com/v1"
    assert values["AGENDA_IA_MODEL"] == "deepseek-v4.1-flash-abliterated"
    assert 1 <= int(values["AGENDA_IA_MAX_CHAMADAS_LOTE"]) <= 5
    assert 1 <= int(values["AGENDA_IA_MAX_CHAMADAS_DIA"]) <= 100


def test_env_example_documenta_backup_e_agenda():
    values = parse_env_example()
    assert values["AGENDA_BACKUP_DIR"]
    assert values["AGENDA_SCHEDULE"] == "config/schedule.json"


@pytest.mark.parametrize("name", [".env"])
def test_env_nao_versionado(name):
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert name in gitignore
