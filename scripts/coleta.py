#!/usr/bin/env python3
"""Rotina de coleta em lote (agendada às 06, 12, 17 e 22 em America/Bahia).

Percorre as fontes **coletáveis** do catálogo (ou uma lista explícita) e
aplica o mesmo contrato de cada coletor: uma requisição por fonte, sem
disfarce de bloqueio, sem retry agressivo. É a única rotina agendada que usa
rede — por isso é isolada aqui, e não dentro do laço do agendador.

Nada aqui publica. Com ``--gravar`` o resultado é persistido pelo núcleo
canônico (``collectors.ingest``), sempre como **pendente** ou **rejeitado**;
aprovar é ato humano no bot e publicar exige autorização + frase de
confirmação (ver docs/security.md).

``--ia`` liga o enriquecimento opcional por IA (classificação assistida). Só
funciona com ``--gravar`` e exige ``AGENDA_IA_ENABLED=1`` **e**
``REFUSELESS_API_KEY`` no ambiente; faltando qualquer um, o comando sai com
código 2 em vez de seguir silenciosamente sem IA. Sem ``--ia`` nenhuma chamada
de rede além da coleta acontece.

Falha de uma fonte não interrompe as demais: cada fonte tem seu próprio
resultado, e a saída diz exatamente o que passou e o que falhou.

    python scripts/coleta.py --json
    python scripts/coleta.py --fontes tca,sympla_salvador --limite 20
    python scripts/coleta.py --gravar --json
    python scripts/coleta.py --gravar --db /app/data/agenda.db
    python scripts/coleta.py --gravar --ia --fontes tca --limite 10

Códigos de saída:
    0  pelo menos uma fonte coletou e nenhuma falhou (parcial ainda é 0 se
       houve sucesso e as falhas foram de fonte, não do processo)
    1  nenhuma fonte coletou (todas falharam) ou todas falharam
    2  uso inválido / lista de fontes inválida
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.fetch import ErroColeta  # noqa: E402
from collectors.ingest import FonteNaoColetavel, ingerir_coletados  # noqa: E402
from collectors.registry import obter_coletor  # noqa: E402
from sources.catalog import CATALOGO, FonteCultural, por_id  # noqa: E402


def fontes_disponiveis() -> list[FonteCultural]:
    """Fontes que o catálogo declara como coletáveis (acessibilidade == ok)."""
    return [fonte for fonte in CATALOGO if fonte.coletavel]


def resolver_fontes(ids: list[str] | None) -> list[FonteCultural]:
    """Lista explícita de ids ou, na ausência, todas as coletáveis."""
    if not ids:
        return fontes_disponiveis()
    selecionadas: list[FonteCultural] = []
    vistos: set[str] = set()
    for fonte_id in ids:
        fonte = por_id(fonte_id)  # KeyError se não existir no catálogo
        if fonte.id in vistos:
            continue
        vistos.add(fonte.id)
        selecionadas.append(fonte)
    return selecionadas


def coletar_fonte(fonte: FonteCultural, limite: int | None, conn, ia=None) -> dict:
    """Coleta (e opcionalmente grava) uma única fonte, isolando a falha."""
    item: dict = {
        "fonte": fonte.id,
        "nome": fonte.nome,
        "coletavel": fonte.coletavel,
        "ok": False,
        "total": 0,
        "persistencia": None,
        "erro": None,
    }
    try:
        coletor = obter_coletor(fonte)
        eventos = coletor.coletar(limite=limite)
        item["total"] = len(eventos)
        item["estado_coletor"] = getattr(coletor, "ultimo_estado", {})
        if conn is not None:
            resultado = ingerir_coletados(conn, fonte, eventos, ia=ia)
            item["persistencia"] = resultado.to_dict()
        item["ok"] = True
    except ErroColeta as erro:
        item["erro"] = f"coleta falhou: {erro}"
    except FonteNaoColetavel as erro:
        item["erro"] = f"fonte não coletável: {erro}"
    except (KeyError, ValueError) as erro:
        item["erro"] = f"coletor inválido: {erro}"
    except Exception as erro:  # noqa: BLE001 - uma fonte não derruba o lote
        item["erro"] = f"{type(erro).__name__}: {erro}"
    return item


def _agregar(itens: list[dict]) -> dict:
    totais = {"coletados": 0, "aceitos": 0, "rejeitados": 0, "duplicados": 0}
    for item in itens:
        persistencia = item.get("persistencia") or {}
        for chave in totais:
            totais[chave] += int(persistencia.get(chave) or 0)
    return totais


def _agregar_ia(itens: list[dict]) -> dict:
    """Soma os contadores de IA do lote. Ausente == integração não usada."""
    chaves = ("sugeridas", "ignoradas", "em_cache", "bloqueadas", "falhas", "chamadas")
    total = {chave: 0 for chave in chaves}
    for item in itens:
        resumo = (item.get("persistencia") or {}).get("ia") or {}
        for chave in chaves:
            total[chave] += int(resumo.get(chave) or 0)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coleta", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--fontes", default="",
                        help="ids separados por vírgula (padrão: todas as coletáveis)")
    parser.add_argument("--limite", type=int, default=None,
                        help="máximo de eventos por fonte (padrão: sem limite)")
    parser.add_argument("--gravar", action="store_true",
                        help="persiste no banco canônico (sempre pendente/rejeitado)")
    parser.add_argument("--db", default=None,
                        help="SQLite de destino (padrão: AGENDA_DB ou data/agenda.db)")
    parser.add_argument("--ia", action="store_true",
                        help="enriquecimento opcional por IA (exige AGENDA_IA_ENABLED=1 "
                             "e REFUSELESS_API_KEY; requer --gravar)")
    parser.add_argument("--json", action="store_true", help="saída JSON")
    args = parser.parse_args(argv)

    if args.limite is not None and args.limite < 0:
        print(json.dumps({"erro": "limite não pode ser negativo"}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.ia and not args.gravar:
        print(json.dumps(
            {"erro": "--ia exige --gravar: sugestão sem evento persistido não tem onde morar"},
            ensure_ascii=False), file=sys.stderr)
        return 2

    enriquecimento = None
    if args.ia:
        from ia import EnriquecimentoIA, IaConfig  # noqa: PLC0415 - opcional

        config_ia = IaConfig.from_env()
        if not config_ia.habilitada:
            print(json.dumps(
                {"erro": "integração de IA desligada",
                 "motivo": config_ia.motivo_desabilitada,
                 "exigido": ["AGENDA_IA_ENABLED=1", "REFUSELESS_API_KEY"]},
                ensure_ascii=False), file=sys.stderr)
            return 2

    ids = [parte.strip() for parte in args.fontes.split(",") if parte.strip()]
    try:
        fontes = resolver_fontes(ids)
    except KeyError as erro:
        print(json.dumps({"erro": f"fonte desconhecida: {erro}"}, ensure_ascii=False),
              file=sys.stderr)
        return 2

    if not fontes:
        print(json.dumps({"erro": "nenhuma fonte coletável no catálogo",
                          "fontes_coletaveis": []}, ensure_ascii=False), file=sys.stderr)
        return 1

    conn = None
    try:
        if args.gravar:
            from database import abrir_banco  # noqa: PLC0415 - só quando grava

            import os  # noqa: PLC0415

            conn = abrir_banco(args.db or os.environ.get("AGENDA_DB") or "data/agenda.db")
        if args.ia and conn is not None:
            enriquecimento = EnriquecimentoIA(conn, config_ia)
        itens = [coletar_fonte(fonte, args.limite, conn, enriquecimento) for fonte in fontes]
    finally:
        if conn is not None:
            conn.close()

    sucessos = [i for i in itens if i["ok"]]
    relatorio = {
        "iniciado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gravado": bool(args.gravar),
        "ia": bool(args.ia),
        "limite": args.limite,
        "total_fontes": len(itens),
        "fontes_ok": len(sucessos),
        "fontes_com_falha": len(itens) - len(sucessos),
        "total_eventos": sum(i["total"] for i in itens),
        **_agregar(itens),
        "ia_resumo": _agregar_ia(itens),
        "fontes": itens,
    }

    if args.json:
        print(json.dumps(relatorio, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"coleta: {len(sucessos)}/{len(itens)} fonte(s) ok, "
              f"{relatorio['total_eventos']} evento(s), gravado={relatorio['gravado']}")
        for item in itens:
            estado = "OK" if item["ok"] else "FALHA"
            detalhe = item["erro"] or f"{item['total']} evento(s)"
            print(f"  [{estado}] {item['fonte']}: {detalhe}")

    # Nenhuma fonte com sucesso = falha real (rede/catálogo). Sucesso parcial é
    # reportado no relatório e no código 0: fonte bloqueada não é bug do job.
    return 0 if sucessos else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
