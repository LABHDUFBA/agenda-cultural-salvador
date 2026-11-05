#!/usr/bin/env python3
"""Importa eventos de um CSV externo (data_evento,hora,fonte,titulo,local,preco,resumo,url)
pela API oficial de ingestão e aprova em massa com auditoria.

Regras:
  * Confiança "media": data vem da coluna, mas hora/local podem estar vazios
    (metainformação registrada em motivos).
  * Eventos fora de Salvador (ex.: "Santo Antônio de Jesus/BA") entram com
    municipio extraído quando detectável fora do padrão.
  * Nada é aprovado pela ingestão; a aprovação em massa é um passo à parte,
    logado em auditoria com ator 'importacao-csv'.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from database.connection import conectar  # noqa: E402
from database.models import (  # noqa: E402
    Evento,
    Fonte,
    Proveniencia,
    agora_utc,
    para_dt,
)
from database.repository import (  # noqa: E402
    finalizar_sessao,
    ingerir_evento,
    iniciar_sessao,
    registrar_fonte,
)
from collectors.models import EventoColetado  # noqa: E402

FONTE_CSV = "Importação CSV manual"
CONFIANCA = "media"
METODO = "texto_agenda"

# Mapeia o campo `fonte` do CSV para nome canônico de fonte (visível na
# proveniência); desconhecidos caem no genérico.
ALIASES = {
    "elcabong": "El Cabong (agenda cultural)",
    "pelourinho": "Pelourinho Dia e Noite (SECULT)",
    "tca": "Teatro Castro Alves — Calendário de Eventos",
    "agendaculturalsalvador": "Agenda Cultural Salvador (portal)",
    "ibahia": "iBahia",
    "atarde": "A Tarde",
    "correio24h": "Correio 24h",
    "salvadordabahia": "Salvador da Bahia (turismo)",
}

# Municípios fora de Salvador detectáveis no título/local — ficam registrados
# honestamente, mas fora do escopo do canal (status rejeitado pela validação).
FORA_DE_SALVADOR = ("santo antônio de jesus", "lauro de freitas", "camaçari",
                    "simões filho", "dias d'ávila")


def limpar(v: str | None) -> str:
    return (v or "").strip()


def detectar_municipio(titulo: str, local: str) -> str:
    blob = f"{titulo} {local}".lower()
    if "lauro de freitas" in blob:
        return "Lauro de Freitas"
    if "santo antônio de jesus" in blob:
        return "Santo Antônio de Jesus"
    return "Salvador"


def principal(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="importar_csv")
    ap.add_argument("csv_path")
    ap.add_argument("--db", default=str(HERE / "data" / "agenda.db"))
    ap.add_argument("--aprovar", action="store_true",
                    help="após importar, aprova em massa os eventos desta sessão")
    args = ap.parse_args(argv)

    conn = conectar(args.db)
    with open(args.csv_path, encoding="utf-8-sig") as fh:
        leitor = csv.DictReader(fh)
        # renomeia a primeira coluna se vier com BOM
        leitor.fieldnames = [
            (f.lstrip("\ufeff").strip() or f) for f in (leitor.fieldnames or [])
        ]
        linhas = list(leitor)

    sessoes_por_fonte: dict[str, int] = {}
    fonte_ids: dict[str, int] = {}
    aceitos = rejeitados = duplicados = erros = 0
    ids_importados: list[int] = []

    for linha in linhas:
        if not linha:
            continue
        linha = {k: v for k, v in linha.items() if k is not None}
        get = linha.get
        titulo = limpar(linha.get("titulo"))
        data = limpar(linha.get("data_evento"))
        url = limpar(linha.get("url"))
        if not titulo or not data or not url:
            erros += 1
            continue
        fonte_csv = limpar(linha.get("fonte")) or "desconhecida"
        nome_fonte = ALIASES.get(fonte_csv, f"CSV externo — {fonte_csv}")

        if nome_fonte not in fonte_ids:
            partes = url.split("/")
            base = partes[0] + "//" + partes[2] if len(partes) > 2 else url
            achou = conn.execute(
                "SELECT id FROM fontes WHERE nome = ?", (nome_fonte,)
            ).fetchone()
            if achou is not None:
                fonte_ids[nome_fonte] = int(achou["id"])
            else:
                fonte_ids[nome_fonte] = registrar_fonte(conn, Fonte(
                    nome=nome_fonte,
                    url_base=base,
                    tipo="site",
                    confiabilidade="media",
                ))
        if nome_fonte not in sessoes_por_fonte:
            sessoes_por_fonte[nome_fonte] = iniciar_sessao(conn, fonte_ids[nome_fonte])

        hora = limpar(linha.get("hora"))
        if hora:
            try:
                h, m = hora.split(":")[:2]
                inicio = f"{data}T{int(h):02d}:{int(m):02d}"
            except ValueError:
                inicio = data
        else:
            inicio = data

        local = limpar(linha.get("local"))
        resumo = limpar(linha.get("resumo"))
        preco = limpar(linha.get("preco"))

        try:
            coletado = EventoColetado(
                fonte_id=nome_fonte,
                titulo=titulo,
                inicio_em=para_dt(inicio),
                url_original=url,
                metodo_extracao=METODO,
                confianca=CONFIANCA,
                trecho=(resumo or titulo)[:500],
                descricao=resumo,
                local_nome=local,
                preco=preco,
                campos_inferidos=("hora",) if not hora else (),
            )
        except ValueError as exc:
            erros += 1
            print(f"  pulando '{titulo[:40]}': {exc}")
            continue

        evento = Evento(
            titulo=titulo,
            inicio_em=para_dt(inicio),
            municipio=detectar_municipio(titulo, local),
            uf="BA",
            local_nome=local,
            descricao=resumo,
            preco=preco,
            url_ingresso=url if "ingresso" not in url.lower() else "",
        )
        prov = Proveniencia(
            fonte_id=fonte_ids[nome_fonte],
            url_original=url,
            sessao_id=sessoes_por_fonte[nome_fonte],
            hash_conteudo=hashlib.sha256((titulo + url).encode()).hexdigest(),
            trecho=(resumo or titulo)[:500],
        )
        motivos = []
        if not hora:
            motivos.append("hora_ausente_na_fonte")
        if not local:
            motivos.append("local_ausente_na_fonte")

        res = ingerir_evento(conn, evento, prov, motivos_extra=motivos)
        if res.status == "pendente":
            aceitos += 1
            if res.evento_id:
                ids_importados.append(res.evento_id)
        elif res.status == "duplicado":
            duplicados += 1
        else:
            rejeitados += 1

    for nome, sid in sessoes_por_fonte.items():
        finalizar_sessao(conn, sid)

    print(json.dumps({
        "aceitos": aceitos, "duplicados": duplicados,
        "rejeitados": rejeitados, "erros": erros,
        "ids": len(ids_importados),
    }, ensure_ascii=False))

    if args.aprovar and ids_importados:
        from database.connection import transacao
        agora = agora_utc().isoformat(timespec="seconds")
        with transacao(conn):
            marks = ",".join("?" * len(ids_importados))
            cur = conn.execute(
                f"UPDATE eventos SET status='aprovado', "
                f"atualizado_em=? WHERE id IN ({marks}) AND status='pendente'",
                (agora, *ids_importados),
            )
            from database.repository import registrar_auditoria
            registrar_auditoria(
                conn, entidade="evento", entidade_id=0,
                acao="aprovacao_em_massa",
                estado_novo="aprovado",
                detalhe=f"importacao_csv: {cur.rowcount} eventos aprovados por decisao humana",
            )
        print(json.dumps({"aprovados": cur.rowcount}))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
