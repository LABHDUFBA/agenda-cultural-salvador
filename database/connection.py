"""Conexão e inicialização do schema SQLite."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .erros import ErroBanco

# v3: tabelas `feiras`, `feiras_fontes` e `feiras_evidencias` (aditivo; nenhuma
# tabela ou coluna existente foi alterada).
SCHEMA_VERSION = 3
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Colunas adicionadas depois da versão 1. `CREATE TABLE IF NOT EXISTS` não
# altera tabela existente, então bancos antigos recebem ALTER TABLE ADD COLUMN
# (aditivo: nenhuma linha é reescrita ou removida).
COLUNAS_ADICIONADAS = {
    "url_fonte": "TEXT NOT NULL DEFAULT ''",
    "enviado_por": "INTEGER NOT NULL DEFAULT 0",
    "autorizacao_confirmada": "INTEGER NOT NULL DEFAULT 0",
    "autorizacao_referencia": "TEXT NOT NULL DEFAULT ''",
}


def conectar(caminho: str | Path = ":memory:") -> sqlite3.Connection:
    """Abre uma conexão com as PRAGMAs do projeto.

    `caminho` aceita um arquivo ou ":memory:". O chamador é dono da conexão
    e deve fechá-la.
    """
    conn = sqlite3.connect(str(caminho))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _migrar_colunas(conn: sqlite3.Connection) -> list[str]:
    """Adiciona colunas ausentes em `eventos` sem tocar nos dados existentes."""
    existentes = {linha["name"] for linha in conn.execute("PRAGMA table_info(eventos)")}
    adicionadas: list[str] = []
    for nome, ddl in COLUNAS_ADICIONADAS.items():
        if nome not in existentes:
            conn.execute(f"ALTER TABLE eventos ADD COLUMN {nome} {ddl}")
            adicionadas.append(nome)
    if adicionadas:
        conn.commit()
    return adicionadas


def inicializar(conn: sqlite3.Connection) -> int:
    """Cria as tabelas (idempotente) e devolve a versão do schema.

    Não faz migração destrutiva: se o arquivo tiver versão futura, aborta;
    bancos da versão 1 recebem apenas colunas novas (dados preservados).
    """
    versao = conn.execute("PRAGMA user_version").fetchone()[0]
    if versao > SCHEMA_VERSION:
        raise ErroBanco(
            f"schema {versao} é mais novo que o suportado ({SCHEMA_VERSION})"
        )
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrar_colunas(conn)
    if versao < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return SCHEMA_VERSION


@contextmanager
def transacao(conn: sqlite3.Connection):
    """Transação explícita: COMMIT no sucesso, ROLLBACK em qualquer exceção."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def abrir_banco(caminho: str | Path = ":memory:") -> sqlite3.Connection:
    """Conveniência: conecta e inicializa o schema."""
    conn = conectar(caminho)
    inicializar(conn)
    return conn
