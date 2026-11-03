"""CLI de sondagem, coleta manual e fila de revisão.

    python -m collectors.cli list
    python -m collectors.cli probe --json
    python -m collectors.cli probe --fonte tca --salvar
    python -m collectors.cli collect --fonte tca --limite 5 --json
    python -m collectors.cli collect --fonte tca --gravar --db data/agenda.db
    python -m collectors.cli pendentes --db data/agenda.db --json

Sem ``--gravar`` nada é escrito no banco: a saída é JSON/dict para inspeção.
``--gravar`` persiste pelo núcleo canônico (``collectors.ingest``), sempre como
``pendente``/``rejeitado`` — nunca aprova e nunca publica. ``probe --salvar``
escreve ``sources/probe_report.json`` com o resultado real da última sondagem
(evidência datada).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sources.catalog import CATALOGO, por_id, resumo

from .fetch import ErroColeta, HttpFetcher
from .registry import obter_coletor

RAIZ = Path(__file__).resolve().parent.parent
RELATORIO = RAIZ / "sources" / "probe_report.json"
BANCO_PADRAO = os.environ.get("AGENDA_DB") or "data/agenda.db"


def _imprimir_tabela(linhas: list[dict[str, object]]) -> None:
    largura = max((len(str(linha["fonte_id"])) for linha in linhas), default=8)
    print(f"{'fonte':<{largura}}  {'status':>6}  {'acessível':<9}  {'bytes':>8}  detalhe")
    for linha in linhas:
        detalhe = linha.get("erro") or linha.get("observacao") or ""
        print(
            f"{linha['fonte_id']:<{largura}}  {str(linha.get('status') or '-'):>6}  "
            f"{'sim' if linha.get('acessivel') else 'não':<9}  "
            f"{str(linha.get('tamanho') or '-'):>8}  {str(detalhe)[:70]}"
        )


def cmd_list(args: argparse.Namespace) -> int:
    for fonte in CATALOGO:
        marca = "coletável" if fonte.coletavel else "sem coleta"
        print(
            f"{fonte.id:<24} {fonte.escopo:<10} {fonte.tipo_conteudo:<16} "
            f"{fonte.acessibilidade:<13} {marca:<10} {fonte.url}"
        )
    print("\nresumo:", ", ".join(f"{k}={v}" for k, v in sorted(resumo().items())))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    fetcher = HttpFetcher()
    fontes = [por_id(args.fonte)] if args.fonte else list(CATALOGO)
    resultados: list[dict[str, object]] = []
    for fonte in fontes:
        coletor = obter_coletor(fonte, fetcher)
        resultado = coletor.verificar_acesso()
        resultado.setdefault("observacao", fonte.observacao)
        resultados.append(resultado)
        if not args.json:
            estado = "OK" if resultado.get("acessivel") else "FALHA"
            print(f"[{estado}] {fonte.id}: {resultado}")

    if args.json:
        print(json.dumps(resultados, ensure_ascii=False, indent=2, default=str))
    if args.salvar:
        RELATORIO.parent.mkdir(parents=True, exist_ok=True)
        RELATORIO.write_text(
            json.dumps(
                {
                    "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "resultados": resultados,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nrelatório salvo em {RELATORIO}")
    return 0 if all(r.get("acessivel") for r in resultados) else 1


def cmd_collect(args: argparse.Namespace) -> int:
    fonte = por_id(args.fonte)
    coletor = obter_coletor(fonte)
    try:
        eventos = coletor.coletar(limite=args.limite)
    except ErroColeta as erro:
        print(json.dumps(erro.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2

    estado = getattr(coletor, "ultimo_estado", {})

    resumo_persistencia = None
    if args.gravar:
        from database import abrir_banco
        from .ingest import FonteNaoColetavel, ingerir_coletados

        conn = abrir_banco(args.db or BANCO_PADRAO)
        try:
            resultado = ingerir_coletados(conn, fonte, eventos)
        except FonteNaoColetavel as exc:
            print(json.dumps({"erro": str(exc), "fonte": fonte.id},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        finally:
            conn.close()
        resumo_persistencia = resultado.to_dict()
        if resultado.erros:
            print(json.dumps({"erros_de_ingestao": list(resultado.erros)},
                             ensure_ascii=False), file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {
                    "fonte_id": fonte.id,
                    "coletado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "estado": estado,
                    "total": len(eventos),
                    "persistencia": resumo_persistencia,
                    "eventos": [e.to_dict() for e in eventos],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"{fonte.nome} — {len(eventos)} evento(s)  {estado}")
    if resumo_persistencia:
        print(
            "gravado: sessao={sessao_id} coletados={coletados} aceitos={aceitos} "
            "rejeitados={rejeitados} duplicados={duplicados} (todos pendentes)".format(
                **resumo_persistencia
            )
        )
    for evento in eventos:
        inferido = f" [inferido: {','.join(evento.campos_inferidos)}]" if evento.campos_inferidos else ""
        print(
            f"  {evento.inicio_em:%d/%m/%Y %H:%M}  {evento.titulo[:60]:<60} "
            f"{evento.local_nome[:28]:<28} {evento.confianca}{inferido}"
        )
    return 0


def cmd_pendentes(args: argparse.Namespace) -> int:
    """Fila de revisão canônica (somente leitura)."""
    from database import abrir_banco
    from .ingest import fila_de_revisao

    conn = abrir_banco(args.db or BANCO_PADRAO)
    try:
        fila = fila_de_revisao(conn, limite=args.limite or 50)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(fila, ensure_ascii=False, indent=2, default=str))
        return 0
    if not fila:
        print("fila de revisão vazia")
        return 0
    print(f"{len(fila)} evento(s) pendente(s) — nada é publicado sem aprovação humana")
    for evento in fila:
        fonte = evento["proveniencia"][0]["url_original"] if evento["proveniencia"] else "-"
        print(
            f"  #{evento['id']:<5} {evento['inicio_em']}  {evento['titulo'][:52]:<52} "
            f"{evento['local_nome'][:24]:<24} {evento['motivo'][:40]:<40} {fonte}"
        )
    return 0


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collectors", description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)

    lista = sub.add_parser("list", help="lista o catálogo de fontes")
    lista.set_defaults(func=cmd_list)

    probe = sub.add_parser("probe", help="testa acesso às fontes (uma requisição cada)")
    probe.add_argument("--fonte", help="id da fonte (padrão: todas)")
    probe.add_argument("--json", action="store_true", help="saída JSON")
    probe.add_argument("--salvar", action="store_true", help="grava sources/probe_report.json")
    probe.set_defaults(func=cmd_probe)

    coletar = sub.add_parser("collect", help="coleta uma fonte")
    coletar.add_argument("--fonte", required=True, help="id da fonte")
    coletar.add_argument("--limite", type=int, default=None, help="máximo de eventos")
    coletar.add_argument("--json", action="store_true", help="saída JSON")
    coletar.add_argument("--gravar", action="store_true",
                         help="persiste no banco canônico (sempre pendente/rejeitado)")
    coletar.add_argument("--db", default=None,
                         help=f"caminho do SQLite (padrão: AGENDA_DB ou {BANCO_PADRAO})")
    coletar.set_defaults(func=cmd_collect)

    pendentes = sub.add_parser("pendentes", help="fila de revisão canônica (somente leitura)")
    pendentes.add_argument("--db", default=None,
                           help=f"caminho do SQLite (padrão: AGENDA_DB ou {BANCO_PADRAO})")
    pendentes.add_argument("--limite", type=int, default=50)
    pendentes.add_argument("--json", action="store_true", help="saída JSON")
    pendentes.set_defaults(func=cmd_pendentes)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
