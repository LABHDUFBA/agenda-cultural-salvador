#!/usr/bin/env python3
"""Exporta os dados de eventos/feiras do banco para o repositório.

Gera, em ``dados/``:
  - ``eventos.csv`` / ``eventos.json``   — todos os eventos, linha por linha;
  - ``feiras.csv``  / ``feiras.json``    — feiras de Salvador;
  - ``resumo.json``                      — contagens por status/fonte/data de corte.

Sanitizado por construção: somente campos públicos (título, datas, local,
categoria, URL da fonte e do ingresso, status editorial).  Nada de dados
pessoais, tokens ou identificadores de usuário do Telegram.

Uso:
    python scripts/exportar.py [--db data/agenda.db] [--saida dados]
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CAMPOS_EVENTO = [
    "id",
    "titulo",
    "descricao",
    "inicio_em",
    "fim_em",
    "data_ref",
    "local_nome",
    "endereco",
    "bairro",
    "municipio",
    "uf",
    "latitude",
    "longitude",
    "categoria",
    "preco",
    "url_ingresso",
    "url_fonte",
    "status",
    "fonte",
    "criado_em",
]

CAMPOS_FEIRA = [
    "id",
    "nome",
    "descricao",
    "bairro",
    "endereco",
    "horario",
    "fonte",
    "url_fonte",
]


def exportar(db_path: Path, saida: Path) -> dict:
    if not db_path.exists():
        print(f"banco não encontrado: {db_path}", file=sys.stderr)
        return {"erro": "banco ausente"}

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.text_factory = lambda b: b.decode("utf-8", errors="replace")
    saida.mkdir(parents=True, exist_ok=True)

    # ---- eventos (fonte via proveniencia) ----
    rows = db.execute(
        """
        SELECT e.id, e.titulo, e.descricao, e.inicio_em, e.fim_em, e.data_ref,
               e.local_nome, e.endereco, e.bairro, e.municipio, e.uf,
               e.latitude, e.longitude, e.categoria, e.preco,
               e.url_ingresso, e.url_fonte, e.status,
               COALESCE((SELECT f.nome FROM proveniencia p
                         JOIN fontes f ON f.id = p.fonte_id
                         WHERE p.evento_id = e.id
                         ORDER BY p.capturado_em DESC LIMIT 1),
                        '') AS fonte,
               e.criado_em
        FROM eventos e
        ORDER BY e.inicio_em
        """
    ).fetchall()
    eventos = [dict(r) for r in rows]

    with (saida / "eventos.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPOS_EVENTO)
        w.writeheader()
        w.writerows(eventos)
    (saida / "eventos.json").write_text(
        json.dumps(eventos, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # ---- feiras ----
    feiras = []
    try:
        frows = db.execute(
            """
            SELECT id, nome, COALESCE(descricao, observacao, '') AS descricao,
                   bairro, endereco, horario,
                   COALESCE(tipo, '') AS fonte, '' AS url_fonte
            FROM feiras
            ORDER BY nome
            """
        ).fetchall()
        feiras = [dict(r) for r in frows]
    except sqlite3.OperationalError:
        pass  # base antiga sem tabela de feiras
    if feiras:
        with (saida / "feiras.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CAMPOS_FEIRA)
            w.writeheader()
            w.writerows(feiras)
        (saida / "feiras.json").write_text(
            json.dumps(feiras, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ---- resumo ----
    por_status = dict(
        db.execute("SELECT status, COUNT(*) FROM eventos GROUP BY status").fetchall()
    )
    por_fonte = dict(
        db.execute(
            """
            SELECT COALESCE(f.nome, '?'), COUNT(DISTINCT p.evento_id)
            FROM proveniencia p JOIN fontes f ON f.id = p.fonte_id
            GROUP BY f.nome
            """
        ).fetchall()
    )
    resumo = {
        "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_eventos": len(eventos),
        "total_feiras": len(feiras),
        "por_status": por_status,
        "por_fonte": por_fonte,
        "db": str(db_path),
    }
    (saida / "resumo.json").write_text(
        json.dumps(resumo, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    db.close()
    return resumo


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="exportar")
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--db", default=str(here / "data" / "agenda.db"))
    ap.add_argument("--saida", default=str(here / "dados"))
    args = ap.parse_args(argv)
    resumo = exportar(Path(args.db), Path(args.saida))
    print(json.dumps(resumo, ensure_ascii=False, indent=1))
    return 0 if "erro" not in resumo else 1


if __name__ == "__main__":
    raise SystemExit(main())
