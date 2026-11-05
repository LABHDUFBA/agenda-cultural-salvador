"""Persistência das sugestões de IA e do consumo de chamadas.

Regras que este módulo garante no banco, não só na intenção:

* sugestão gravada é sempre ``status='sugerida'``; aceitar/descartar é ato
  humano identificado (``decidida_por`` não vazio, exigido pelo CHECK);
* aceitar só escreve colunas de **classificação** numa lista fechada
  (:data:`COLUNAS_ACEITAVEIS`) e apenas em evento ``pendente`` — nunca em
  ``aprovado``, ``rejeitado`` ou ``duplicado``;
* aceitar/descartar deixa trilha em ``auditoria`` (append-only);
* ``ia_chamadas`` conta tentativas reais ('ok'/'erro') por dia, o que faz o
  teto diário ser auditável em vez de declarado.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Sequence

from database import DadoInvalido, TransicaoInvalida, registrar_auditoria, transacao
from database.models import agora_utc, para_iso

from .modelos import SugestaoIA

# Único campo canônico que uma sugestão pode preencher — e só com aceite humano.
COLUNAS_ACEITAVEIS = ("categoria",)

RESULTADOS_CHAMADA = ("ok", "erro", "cache", "limite")
RESULTADOS_COM_CUSTO = ("ok", "erro")


def _csv(valores: Iterable[str]) -> str:
    return ",".join(valores)


def gravar_sugestao(
    conn: sqlite3.Connection,
    sugestao: SugestaoIA,
    *,
    evento_id: int | None = None,
    base_url: str = "",
) -> int:
    """Insere a sugestão como ``sugerida``. Nunca toca em ``eventos``."""
    if len(sugestao.hash_entrada) != 64:
        raise DadoInvalido("hash_entrada deve ser sha256 (64 hex)")
    if not sugestao.modelo.strip():
        raise DadoInvalido("sugestão sem modelo identificado")
    if not 0.0 <= sugestao.confianca <= 1.0:
        raise DadoInvalido("confiança fora de [0, 1]")
    with transacao(conn):
        cursor = conn.execute(
            "INSERT INTO ia_sugestoes ("
            "evento_id, provedor, base_url, modelo, prompt_versao, hash_entrada, "
            "url_original, trecho_entrada, saida_json, categoria, publico, "
            "faixa_etaria, gratuito, acessibilidade, tags, resumo, confianca, "
            "justificativa, campos_incertos, status, criada_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'sugerida', ?)",
            (
                evento_id,
                sugestao.provedor,
                base_url,
                sugestao.modelo,
                sugestao.prompt_versao,
                sugestao.hash_entrada,
                sugestao.url_original,
                sugestao.trecho_entrada,
                sugestao.saida_json or "{}",
                sugestao.categoria,
                sugestao.publico,
                sugestao.faixa_etaria,
                None if sugestao.gratuito is None else int(sugestao.gratuito),
                _csv(sugestao.acessibilidade),
                _csv(sugestao.tags),
                sugestao.resumo,
                sugestao.confianca,
                sugestao.justificativa,
                _csv(sugestao.campos_incertos),
                para_iso(agora_utc()),
            ),
        )
        return int(cursor.lastrowid or 0)


def registrar_chamada(
    conn: sqlite3.Connection,
    *,
    modelo: str,
    hash_entrada: str,
    resultado: str,
    evento_id: int | None = None,
    detalhe: str = "",
    duracao_ms: int = 0,
) -> int:
    """Registra a tentativa (inclusive falha) para o teto diário ser auditável."""
    if resultado not in RESULTADOS_CHAMADA:
        raise DadoInvalido(f"resultado de chamada inválido: {resultado!r}")
    if len(hash_entrada) != 64:
        raise DadoInvalido("hash_entrada deve ser sha256 (64 hex)")
    with transacao(conn):
        cursor = conn.execute(
            "INSERT INTO ia_chamadas "
            "(modelo, hash_entrada, evento_id, resultado, detalhe, duracao_ms, criada_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                modelo,
                hash_entrada,
                evento_id,
                resultado,
                str(detalhe or "")[:300],
                max(0, int(duracao_ms)),
                para_iso(agora_utc()),
            ),
        )
        return int(cursor.lastrowid or 0)


def chamadas_no_dia(conn: sqlite3.Connection, *, dia: date | None = None) -> int:
    """Tentativas que custaram chamada ('ok'/'erro') no dia UTC informado."""
    dia = dia or agora_utc().date()
    inicio = datetime.combine(dia, time.min, tzinfo=timezone.utc)
    fim = inicio + timedelta(days=1)
    linhas = conn.execute(
        "SELECT count(*) FROM ia_chamadas "
        "WHERE resultado IN ('ok', 'erro') AND criada_em >= ? AND criada_em < ?",
        (para_iso(inicio), para_iso(fim)),
    ).fetchone()
    return int(linhas[0])


def sugestao_por_hash(
    conn: sqlite3.Connection, hash_entrada: str, *, aceitar_descartada: bool = False
) -> dict | None:
    """Cache: a mesma entrada já classificada não deve custar outra chamada."""
    if len(hash_entrada) != 64:
        return None
    filtro = "" if aceitar_descartada else "AND status <> 'descartada' "
    linha = conn.execute(
        "SELECT * FROM ia_sugestoes WHERE hash_entrada = ? "
        f"{filtro}ORDER BY id DESC LIMIT 1",
        (hash_entrada,),
    ).fetchone()
    return dict(linha) if linha is not None else None


def sugestoes_do_evento(conn: sqlite3.Connection, evento_id: int) -> list[dict]:
    return [
        dict(linha)
        for linha in conn.execute(
            "SELECT * FROM ia_sugestoes WHERE evento_id = ? ORDER BY id DESC",
            (evento_id,),
        )
    ]


def listar_sugestoes(
    conn: sqlite3.Connection, *, status: str | None = None, limite: int = 50
) -> list[dict]:
    if limite <= 0:
        raise DadoInvalido("limite deve ser positivo")
    if status is not None and status not in ("sugerida", "aceita", "descartada"):
        raise DadoInvalido(f"status inválido: {status!r}")
    sql = "SELECT * FROM ia_sugestoes"
    parametros: list = []
    if status is not None:
        sql += " WHERE status = ?"
        parametros.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    parametros.append(int(limite))
    return [dict(linha) for linha in conn.execute(sql, parametros)]


def _sugestao_aberta(conn: sqlite3.Connection, sugestao_id: int) -> dict:
    linha = conn.execute(
        "SELECT * FROM ia_sugestoes WHERE id = ?", (int(sugestao_id),)
    ).fetchone()
    if linha is None:
        raise DadoInvalido(f"sugestão {sugestao_id} não encontrada")
    dados = dict(linha)
    if dados["status"] != "sugerida":
        raise TransicaoInvalida(
            f"sugestão {sugestao_id} já foi {dados['status']} por {dados['decidida_por']!r}"
        )
    return dados


def _ator(ator: str) -> str:
    limpo = (ator or "").strip()
    if not limpo:
        raise DadoInvalido("ator é obrigatório (aceite/descarte é ato humano)")
    return limpo[:120]


def aceitar_sugestao(
    conn: sqlite3.Connection,
    sugestao_id: int,
    *,
    ator: str,
    campos: Sequence[str] = ("categoria",),
) -> dict:
    """Aplica campos de classificação ao evento pendente, com auditoria.

    Não existe parâmetro para status: aprovar continua sendo
    :func:`database.aprovar_evento`, chamado por um administrador no bot.
    """
    ator = _ator(ator)
    pedidos = tuple(dict.fromkeys(campos or ()))
    if not pedidos:
        raise DadoInvalido("informe ao menos um campo")
    invalidos = [campo for campo in pedidos if campo not in COLUNAS_ACEITAVEIS]
    if invalidos:
        raise DadoInvalido(
            "campos não aceitáveis: " + ",".join(invalidos)
            + " (aceitáveis: " + ",".join(COLUNAS_ACEITAVEIS) + ")"
        )

    with transacao(conn):
        sugestao = _sugestao_aberta(conn, sugestao_id)
        evento_id = sugestao["evento_id"]
        if evento_id is None:
            raise DadoInvalido("sugestão sem evento associado")
        linha = conn.execute(
            "SELECT * FROM eventos WHERE id = ?", (int(evento_id),)
        ).fetchone()
        if linha is None:
            raise DadoInvalido(f"evento {evento_id} não encontrado")
        evento = dict(linha)
        if evento["status"] != "pendente":
            raise TransicaoInvalida(
                f"evento {evento_id} está {evento['status']!r}: "
                "sugestão só se aplica a evento pendente"
            )

        novos: dict[str, str] = {}
        for campo in pedidos:
            valor = str(sugestao[campo] or "").strip()
            if not valor:
                raise DadoInvalido(f"sugestão não tem valor para {campo!r}")
            novos[campo] = valor[:200]

        conn.execute(
            "UPDATE eventos SET "
            + ", ".join(f"{campo} = ?" for campo in novos)
            + ", atualizado_em = ? WHERE id = ?",
            (*novos.values(), para_iso(agora_utc()), int(evento_id)),
        )
        conn.execute(
            "UPDATE ia_sugestoes SET status = 'aceita', decidida_em = ?, "
            "decidida_por = ? WHERE id = ?",
            (para_iso(agora_utc()), ator, int(sugestao_id)),
        )
        registrar_auditoria(
            conn,
            entidade="evento",
            entidade_id=int(evento_id),
            acao="ia_sugestao_aceita",
            ator=ator,
            estado_anterior=json.dumps(
                {campo: evento[campo] for campo in novos}, ensure_ascii=False
            ),
            estado_novo=json.dumps(novos, ensure_ascii=False),
            detalhe=f"sugestao={sugestao_id};modelo={sugestao['modelo']};"
            f"hash_entrada={sugestao['hash_entrada']}",
        )
    return {"evento_id": evento_id, "sugestao_id": int(sugestao_id), "aplicados": novos}


def descartar_sugestao(conn: sqlite3.Connection, sugestao_id: int, *, ator: str) -> dict:
    ator = _ator(ator)
    with transacao(conn):
        sugestao = _sugestao_aberta(conn, sugestao_id)
        conn.execute(
            "UPDATE ia_sugestoes SET status = 'descartada', decidida_em = ?, "
            "decidida_por = ? WHERE id = ?",
            (para_iso(agora_utc()), ator, int(sugestao_id)),
        )
        registrar_auditoria(
            conn,
            entidade="evento",
            entidade_id=sugestao["evento_id"],
            acao="ia_sugestao_descartada",
            ator=ator,
            estado_anterior="sugerida",
            estado_novo="descartada",
            detalhe=f"sugestao={sugestao_id};modelo={sugestao['modelo']}",
        )
    return {"sugestao_id": int(sugestao_id), "status": "descartada", "ator": ator}


def resumo_consumo(conn: sqlite3.Connection) -> dict:
    linha = conn.execute(
        "SELECT count(*) AS total, "
        "coalesce(sum(resultado = 'ok'), 0) AS ok, "
        "coalesce(sum(resultado = 'erro'), 0) AS erro, "
        "coalesce(sum(resultado = 'cache'), 0) AS cache, "
        "coalesce(sum(resultado = 'limite'), 0) AS limite "
        "FROM ia_chamadas"
    ).fetchone()
    return {
        "total": int(linha["total"]),
        "ok": int(linha["ok"]),
        "erro": int(linha["erro"]),
        "cache": int(linha["cache"]),
        "limite": int(linha["limite"]),
        "hoje": chamadas_no_dia(conn),
    }


__all__ = [
    "COLUNAS_ACEITAVEIS",
    "RESULTADOS_CHAMADA",
    "RESULTADOS_COM_CUSTO",
    "aceitar_sugestao",
    "chamadas_no_dia",
    "descartar_sugestao",
    "gravar_sugestao",
    "listar_sugestoes",
    "registrar_chamada",
    "resumo_consumo",
    "sugestao_por_hash",
    "sugestoes_do_evento",
]
