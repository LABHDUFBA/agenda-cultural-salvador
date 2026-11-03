"""CLI das feiras: carregar catálogo, consultar por dia/bairro, conferir provas.

    python -m feiras.cli carregar --db data/agenda.db
    python -m feiras.cli listar --dia hoje
    python -m feiras.cli listar --bairro Liberdade --json
    python -m feiras.cli buscar pelourinho
    python -m feiras.cli evidencias sao-joaquim
    python -m feiras.cli fontes
    python -m feiras.cli resumo
    python -m feiras.cli verificar --salvar

Sem ``--db`` usa ``AGENDA_DB`` ou ``data/agenda.db``. ``verificar`` sonda as
URLs das fontes com o ``HttpFetcher`` do projeto (UA identificado, robots.txt
respeitado, sem retry em bloqueio) e apenas **relata** — não altera dado.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from database.connection import abrir_banco, conectar, inicializar
from database.models import FUSO_BAHIA

from . import catalogo, repository

RAIZ = Path(__file__).resolve().parent.parent
RELATORIO = RAIZ / "sources" / "probe_report_feiras.json"
BANCO_PADRAO = os.environ.get("AGENDA_DB") or "data/agenda.db"

_DIA_ABREV = {
    0: "segunda", 1: "terca", 2: "quarta", 3: "quinta",
    4: "sexta", 5: "sabado", 6: "domingo",
}


def _hoje_bahia() -> str:
    return _DIA_ABREV[datetime.now(FUSO_BAHIA).weekday()]


def _abrir(caminho: str):
    if caminho == ":memory:":
        return abrir_banco(caminho)
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    conn = conectar(caminho)
    inicializar(conn)
    return conn


def _tabela(linhas: list[repository.FeiraRegistro]) -> None:
    if not linhas:
        print("(nenhuma feira)")
        return
    larg_nome = max(len(f.nome) for f in linhas)
    print(f"{'feira':<{larg_nome}}  {'tipo':<10}  {'bairro':<22}  {'dias':<34}  status")
    for f in linhas:
        bairro = f.bairro or "—"
        print(f"{f.nome:<{larg_nome}}  {f.tipo:<10}  {bairro:<22}  {f.dias_texto:<34}  {f.status_verificacao}")


def cmd_carregar(args: argparse.Namespace) -> int:
    with _abrir(args.db) as conn:
        res = repository.carregar(conn)
        print(
            f"catálogo carregado: {res.criadas} criada(s), {res.atualizadas} "
            f"atualizada(s), {res.inalteradas} inalterada(s), "
            f"{res.evidencias_novas} evidência(s) nova(s), {res.fontes} fonte(s)"
        )
        print("resumo:", json.dumps(repository.resumo(conn), ensure_ascii=False))
    return 0


def cmd_listar(args: argparse.Namespace) -> int:
    dia = args.dia
    if dia and dia.lower() == "hoje":
        dia = _hoje_bahia()
    if dia:
        dia = catalogo.normalizar_dia(dia) or dia.lower()
    with _abrir(args.db) as conn:
        if conn.execute("SELECT count(*) FROM feiras").fetchone()[0] == 0:
            print("banco sem feiras; rode: python -m feiras.cli carregar", file=sys.stderr)
            return 2
        linhas = repository.listar(
            conn, dia=dia, bairro=args.bairro, tipo=args.tipo,
            apenas_regulares=args.regulares, limite=args.limite,
        )
    if args.json:
        print(json.dumps([dataclasses.asdict(f) for f in linhas], ensure_ascii=False, indent=2))
    else:
        if dia:
            print(f"# feiras com funcionamento publicado em {dia}\n")
        _tabela(linhas)
    return 0


def cmd_buscar(args: argparse.Namespace) -> int:
    with _abrir(args.db) as conn:
        linhas = repository.buscar(conn, args.termo, limite=args.limite)
    if args.json:
        print(json.dumps([dataclasses.asdict(f) for f in linhas], ensure_ascii=False, indent=2))
    else:
        _tabela(linhas)
    return 0 if linhas else 1


def cmd_evidencias(args: argparse.Namespace) -> int:
    with _abrir(args.db) as conn:
        feira = repository.obter(conn, args.feira)
        if feira is None:
            print(f"feira não encontrada: {args.feira}", file=sys.stderr)
            return 1
        provas = repository.evidencias(conn, feira.id)
    print(f"#{feira.id} {feira.nome}")
    print(f"  tipo={feira.tipo} bairro={feira.bairro or '—'} ({feira.bairro_base})")
    print(f"  endereço={feira.endereco or '—'}")
    print(f"  dias={feira.dias_texto} horário={feira.horario or '—'}")
    print(f"  status={feira.status_verificacao}")
    if feira.observacao:
        print(f"  obs: {feira.observacao}")
    print("\nprovas:")
    for p in provas:
        print(f"  [{p.campo}] {p.fonte} ({p.confiabilidade}, capturado {p.capturado_em})")
        print(f"    \"{p.trecho}\"")
        print(f"    {p.fonte_url}")
    return 0


def cmd_fontes(args: argparse.Namespace) -> int:
    with _abrir(args.db) as conn:
        linhas = repository.fontes(conn)
    if args.json:
        print(json.dumps(linhas, ensure_ascii=False, indent=2))
        return 0
    for f in linhas:
        status = f["http_status"] if f["http_status"] is not None else "—"
        print(f"{f['chave']:<12} {f['acessibilidade']:<13} http={status:<5} "
              f"{f['metodo_captura']:<17} {f['nome']}")
        print(f"             {f['url']}")
        if f["observacao"]:
            print(f"             obs: {f['observacao']}")
    return 0


def cmd_resumo(args: argparse.Namespace) -> int:
    with _abrir(args.db) as conn:
        print(json.dumps(repository.resumo(conn), ensure_ascii=False, indent=2))
    return 0


def cmd_verificar(args: argparse.Namespace) -> int:
    """Sonda cada fonte agora. Só relata — não grava no banco."""
    from collectors.fetch import HttpFetcher

    fetcher = HttpFetcher()
    resultados: list[dict[str, object]] = []
    for chave, fonte in catalogo.FONTES.items():
        resultado = fetcher.sondar(fonte.url, fonte_id=f"feira:{chave}")
        resultados.append(
            {
                "chave": chave,
                "url": fonte.url,
                "acessivel": resultado.get("acessivel"),
                "status": resultado.get("status"),
                "tamanho": resultado.get("tamanho"),
                "erro": resultado.get("erro"),
                "snapshot": fonte.snapshot,
                "acessibilidade_declarada": fonte.acessibilidade,
            }
        )
        if not args.json:
            estado = "OK" if resultado.get("acessivel") else "FALHA"
            detalhe = resultado.get("erro") or f"{resultado.get('status')} · {resultado.get('tamanho')} bytes"
            print(f"[{estado}] {chave}: {detalhe}")

    if args.json:
        print(json.dumps(resultados, ensure_ascii=False, indent=2, default=str))
    if args.salvar:
        RELATORIO.write_text(
            json.dumps(
                {
                    "gerado_em": datetime.now(FUSO_BAHIA).isoformat(timespec="seconds"),
                    "observacao": (
                        "Sondagem ao vivo das fontes de feiras. Fonte bloqueada/"
                        "indisponível continua no catálogo: o conteúdo citado está "
                        "preservado em sources/feiras/."
                    ),
                    "resultados": resultados,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nrelatório salvo em {RELATORIO}")
    pendentes = [r for r in resultados if not r["acessivel"]]
    if pendentes:
        print(f"\n{len(pendentes)} fonte(s) não alcançável(is) agora: "
              + ", ".join(str(r["chave"]) for r in pendentes))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feiras", description="Feiras de Salvador (dados com evidência)")
    parser.add_argument("--db", default=BANCO_PADRAO, help="caminho do SQLite")
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("carregar", help="sincroniza o catálogo com o banco")
    p.set_defaults(func=cmd_carregar)

    p = sub.add_parser("listar", help="lista feiras")
    p.add_argument("--dia", help="segunda..domingo, abreviação ou 'hoje'")
    p.add_argument("--bairro")
    p.add_argument("--tipo", choices=["livre", "cultural", "artesanato", "misto"])
    p.add_argument("--regulares", action="store_true", help="só feiras de funcionamento regular")
    p.add_argument("--limite", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_listar)

    p = sub.add_parser("buscar", help="busca por nome, bairro ou endereço")
    p.add_argument("termo")
    p.add_argument("--limite", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_buscar)

    p = sub.add_parser("evidencias", help="mostra as provas de uma feira")
    p.add_argument("feira", help="slug ou id")
    p.set_defaults(func=cmd_evidencias)

    p = sub.add_parser("fontes", help="fontes cadastradas e como foram lidas")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fontes)

    p = sub.add_parser("resumo", help="contagens do banco")
    p.set_defaults(func=cmd_resumo)

    p = sub.add_parser("verificar", help="sonda as fontes agora (somente leitura)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--salvar", action="store_true", help="grava sources/probe_report_feiras.json")
    p.set_defaults(func=cmd_verificar)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
