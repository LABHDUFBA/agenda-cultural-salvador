#!/usr/bin/env python3
"""CLI operacional da integração de IA (opcional).

    python -m ia.cli verificar                 # só mostra a configuração
    python -m ia.cli verificar --executar      # UMA chamada real (custa token)
    python -m ia.cli listar --db data/agenda.db --status sugerida
    python -m ia.cli aceitar --db data/agenda.db --id 12 --ator leo
    python -m ia.cli descartar --db data/agenda.db --id 12 --ator leo
    python -m ia.cli consumo --db data/agenda.db --json

Regras da CLI:

* ``verificar`` **não** chama a rede sem ``--executar``: conferir configuração
  não deve custar nada;
* nenhum comando imprime a chave, nem mascarada; o relatório diz apenas
  ``chave_presente``;
* ``aceitar`` aplica somente campos de classificação (hoje: ``categoria``) em
  evento ``pendente``, sempre com ``--ator`` e trilha em auditoria. Aprovar
  continua sendo ``bot/admin.py aprovar``.

Códigos de saída: 0 ok, 1 falha, 2 uso/configuração inválida.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ia import (  # noqa: E402
    ClienteRefuseless,
    ErroConfiguracaoIA,
    ErroIA,
    IaConfig,
    aceitar_sugestao,
    descartar_sugestao,
    listar_sugestoes,
    resumo_consumo,
    texto_de_evento,
)

def _abrir(db: str | None):
    from database import abrir_banco  # noqa: PLC0415 - só quando há banco

    caminho = db or os.environ.get("AGENDA_DB") or "data/agenda.db"
    return abrir_banco(caminho)


def cmd_verificar(args: argparse.Namespace) -> int:
    config = IaConfig.from_env()
    relatorio: dict = {"config": config.resumo(), "executado": False}
    if not args.executar:
        relatorio["aviso"] = "nenhuma chamada feita; use --executar para testar a API"
        print(json.dumps(relatorio, ensure_ascii=False, indent=2))
        return 0
    if not config.habilitada:
        relatorio["erro"] = (
            "integração desligada: " + (config.motivo_desabilitada or "motivo desconhecido")
        )
        print(json.dumps(relatorio, ensure_ascii=False, indent=2))
        return 2
    try:
        cliente = ClienteRefuseless(config)
        entrada = texto_de_evento(
            titulo="Show de samba de roda no Pelourinho",
            local="Largo do Pelourinho",
            descricao="Roda de samba com grupos tradicionais do Recôncavo, entrada franca, "
            "com tradução em Libras.",
            trecho="Sábado, 20h — Samba de Roda, Largo do Pelourinho. Gratuito.",
            url_original="https://exemplo.invalid/agenda/samba-de-roda",
        )
        sugestao, resposta = cliente.classificar(entrada)
    except ErroIA as erro:
        relatorio["erro"] = str(erro)
        print(json.dumps(relatorio, ensure_ascii=False, indent=2))
        return 1
    relatorio.update(
        {
            "executado": True,
            "resposta": resposta.to_dict(),
            "entrada": entrada.resumo(),
            "sugestao": sugestao.to_dict(),
            "motivos": list(sugestao.motivos()),
        }
    )
    print(json.dumps(relatorio, ensure_ascii=False, indent=2))
    return 0


def cmd_listar(args: argparse.Namespace) -> int:
    conn = _abrir(args.db)
    try:
        linhas = listar_sugestoes(conn, status=args.status, limite=args.limite)
        if args.json:
            print(json.dumps(linhas, ensure_ascii=False, indent=2, default=str))
            return 0
        if not linhas:
            print("nenhuma sugestão")
            return 0
        for linha in linhas:
            print(
                f"[{linha['id']}] evento={linha['evento_id']} status={linha['status']} "
                f"categoria={linha['categoria'] or '-'} confianca={linha['confianca']:.2f} "
                f"modelo={linha['modelo']}"
            )
        return 0
    finally:
        conn.close()


def cmd_aceitar(args: argparse.Namespace) -> int:
    conn = _abrir(args.db)
    try:
        campos = tuple(parte.strip() for parte in args.campos.split(",") if parte.strip())
        resultado = aceitar_sugestao(conn, args.id, ator=args.ator, campos=campos)
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
        return 0
    except ValueError as erro:
        print(json.dumps({"erro": str(erro)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        conn.close()


def cmd_descartar(args: argparse.Namespace) -> int:
    conn = _abrir(args.db)
    try:
        print(
            json.dumps(
                descartar_sugestao(conn, args.id, ator=args.ator),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except ValueError as erro:
        print(json.dumps({"erro": str(erro)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        conn.close()


def cmd_consumo(args: argparse.Namespace) -> int:
    conn = _abrir(args.db)
    try:
        dados = resumo_consumo(conn)
        if args.json:
            print(json.dumps(dados, ensure_ascii=False, indent=2))
        else:
            print(
                f"chamadas={dados['total']} ok={dados['ok']} erro={dados['erro']} "
                f"cache={dados['cache']} limite={dados['limite']} hoje={dados['hoje']}"
            )
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ia",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p_ver = sub.add_parser("verificar", help="mostra config; chama a API só com --executar")
    p_ver.add_argument("--executar", action="store_true", help="faz UMA chamada real")
    p_ver.set_defaults(func=cmd_verificar)

    p_list = sub.add_parser("listar", help="lista sugestões gravadas")
    p_list.add_argument("--db", default=None)
    p_list.add_argument("--status", choices=("sugerida", "aceita", "descartada"), default=None)
    p_list.add_argument("--limite", type=int, default=50)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_listar)

    p_ac = sub.add_parser("aceitar", help="aplica campos de classificação (humano)")
    p_ac.add_argument("--db", default=None)
    p_ac.add_argument("--id", type=int, required=True, help="id da sugestão")
    p_ac.add_argument("--ator", required=True, help="quem aceita (auditado)")
    p_ac.add_argument("--campos", default="categoria", help="hoje só: categoria")
    p_ac.set_defaults(func=cmd_aceitar)

    p_de = sub.add_parser("descartar", help="descarta a sugestão (humano)")
    p_de.add_argument("--db", default=None)
    p_de.add_argument("--id", type=int, required=True)
    p_de.add_argument("--ator", required=True)
    p_de.set_defaults(func=cmd_descartar)

    p_cons = sub.add_parser("consumo", help="contadores de uso da API")
    p_cons.add_argument("--db", default=None)
    p_cons.add_argument("--json", action="store_true")
    p_cons.set_defaults(func=cmd_consumo)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ErroConfiguracaoIA as erro:
        print(json.dumps({"erro": str(erro)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError) as erro:
        print(json.dumps({"erro": str(erro)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
