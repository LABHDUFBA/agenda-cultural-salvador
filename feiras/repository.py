"""Persistência e consulta das feiras (tabelas ``feiras*``).

Regras:

* carregar o catálogo é **aditivo**: cria o que falta, atualiza o que mudou e
  nunca apaga feira, fonte ou evidência;
* evidência é append-only (triggers no schema bloqueiam UPDATE/DELETE);
* cada fonte guarda o hash do recorte preservado em ``sources/feiras/``, então
  a prova citada pode ser reconferida offline;
* consulta por dia usa apenas o que foi publicado — feira sem dia publicado
  não aparece em ``listar(dia=...)``.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from database.connection import transacao
from database.models import agora_utc, para_iso
from database.repository import registrar_auditoria

from .catalogo import FEIRAS, FONTES, Feira

RAIZ = Path(__file__).resolve().parent.parent
SNAPSHOTS = RAIZ / "sources" / "feiras"


@dataclass(frozen=True, slots=True)
class FeiraRegistro:
    id: int
    slug: str
    nome: str
    tipo: str
    bairro: str
    bairro_base: str
    endereco: str
    dias: tuple[str, ...]
    horario: str
    feirantes: int | None
    regular: bool
    status_verificacao: str
    observacao: str

    @property
    def dias_texto(self) -> str:
        return ", ".join(self.dias) if self.dias else "não publicado"


@dataclass(frozen=True, slots=True)
class EvidenciaRegistro:
    campo: str
    fonte: str
    fonte_url: str
    trecho: str
    capturado_em: str
    confiabilidade: str


@dataclass(frozen=True, slots=True)
class ResumoCarregamento:
    criadas: int = 0
    atualizadas: int = 0
    inalteradas: int = 0
    evidencias_novas: int = 0
    fontes: int = 0

    @property
    def total(self) -> int:
        return self.criadas + self.atualizadas + self.inalteradas


def _hash_snapshot(nome: str) -> str:
    caminho = SNAPSHOTS / nome
    if not caminho.is_file():
        return ""
    return hashlib.sha256(caminho.read_bytes()).hexdigest()


def _dias_texto(dias: tuple[str, ...]) -> str:
    return ",".join(dias)


def carregar(
    conn: sqlite3.Connection,
    *,
    catalogo: tuple[Feira, ...] = FEIRAS,
    fontes=FONTES,
    raiz_snapshots: Path = SNAPSHOTS,
) -> ResumoCarregamento:
    """Sincroniza o catálogo com o banco. Idempotente e não destrutivo."""
    agora = para_iso(agora_utc())
    criadas = atualizadas = inalteradas = evidencias_novas = 0
    ids_fonte: dict[str, int] = {}

    with transacao(conn):
        for chave, fonte in fontes.items():
            hash_snap = _hash_snapshot(fonte.snapshot)
            caminho = str(raiz_snapshots / fonte.snapshot)
            existente = conn.execute(
                "SELECT id FROM feiras_fontes WHERE chave = ?", (chave,)
            ).fetchone()
            if existente is None:
                cur = conn.execute(
                    "INSERT INTO feiras_fontes (chave, nome, organizacao, url, confiabilidade,"
                    " acessibilidade, http_status, metodo_captura, snapshot_path, hash_snapshot,"
                    " observacao, verificado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        chave, fonte.nome, fonte.organizacao, fonte.url,
                        fonte.confiabilidade, fonte.acessibilidade, fonte.http_status,
                        fonte.metodo_captura, caminho, hash_snap, fonte.observacao, agora,
                    ),
                )
                ids_fonte[chave] = int(cur.lastrowid or 0)
            else:
                ids_fonte[chave] = int(existente["id"])
                conn.execute(
                    "UPDATE feiras_fontes SET nome=?, organizacao=?, url=?, confiabilidade=?,"
                    " acessibilidade=?, http_status=?, metodo_captura=?, snapshot_path=?,"
                    " hash_snapshot=?, observacao=?, verificado_em=? WHERE id=?",
                    (
                        fonte.nome, fonte.organizacao, fonte.url, fonte.confiabilidade,
                        fonte.acessibilidade, fonte.http_status, fonte.metodo_captura,
                        caminho, hash_snap, fonte.observacao, agora, ids_fonte[chave],
                    ),
                )

        for feira in catalogo:
            linha = conn.execute(
                "SELECT * FROM feiras WHERE slug = ?", (feira.slug,)
            ).fetchone()
            valores = (
                feira.nome, feira.tipo, feira.bairro, feira.bairro_base, feira.endereco,
                _dias_texto(feira.dias), feira.horario, feira.feirantes,
                1 if feira.regular else 0, feira.status, feira.observacao,
            )
            if linha is None:
                cur = conn.execute(
                    "INSERT INTO feiras (slug, nome, tipo, bairro, bairro_base, endereco,"
                    " dias, horario, feirantes, regular, status_verificacao, observacao,"
                    " criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (feira.slug, *valores, agora, agora),
                )
                feira_id = int(cur.lastrowid or 0)
                criadas += 1
                registrar_auditoria(
                    conn, entidade="feira", entidade_id=feira_id, acao="criada",
                    estado_novo=feira.nome, detalhe=f"fonte={feira.evidencias[0].fonte}",
                )
            else:
                feira_id = int(linha["id"])
                anterior = (
                    linha["nome"], linha["tipo"], linha["bairro"], linha["bairro_base"],
                    linha["endereco"], linha["dias"], linha["horario"], linha["feirantes"],
                    linha["regular"], linha["status_verificacao"], linha["observacao"],
                )
                if anterior == valores:
                    inalteradas += 1
                else:
                    conn.execute(
                        "UPDATE feiras SET nome=?, tipo=?, bairro=?, bairro_base=?, endereco=?,"
                        " dias=?, horario=?, feirantes=?, regular=?, status_verificacao=?,"
                        " observacao=?, atualizado_em=? WHERE id=?",
                        (*valores, agora, feira_id),
                    )
                    atualizadas += 1
                    registrar_auditoria(
                        conn, entidade="feira", entidade_id=feira_id, acao="atualizada",
                        estado_anterior=linha["nome"], estado_novo=feira.nome,
                    )

            for ev in feira.evidencias:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO feiras_evidencias"
                    " (feira_id, fonte_id, campo, trecho, capturado_em) VALUES (?,?,?,?,?)",
                    (feira_id, ids_fonte[ev.fonte], ev.campo, ev.trecho, agora[:10]),
                )
                evidencias_novas += cur.rowcount

    return ResumoCarregamento(
        criadas=criadas,
        atualizadas=atualizadas,
        inalteradas=inalteradas,
        evidencias_novas=evidencias_novas,
        fontes=len(ids_fonte),
    )


def _linha(row: sqlite3.Row) -> FeiraRegistro:
    dias = tuple(d for d in (row["dias"] or "").split(",") if d)
    return FeiraRegistro(
        id=int(row["id"]),
        slug=row["slug"],
        nome=row["nome"],
        tipo=row["tipo"],
        bairro=row["bairro"],
        bairro_base=row["bairro_base"],
        endereco=row["endereco"],
        dias=dias,
        horario=row["horario"],
        feirantes=row["feirantes"],
        regular=bool(row["regular"]),
        status_verificacao=row["status_verificacao"],
        observacao=row["observacao"],
    )


def listar(
    conn: sqlite3.Connection,
    *,
    dia: str | None = None,
    bairro: str | None = None,
    tipo: str | None = None,
    apenas_regulares: bool = False,
    limite: int | None = None,
) -> list[FeiraRegistro]:
    sql = "SELECT * FROM feiras WHERE 1=1"
    params: list[object] = []
    if dia:
        sql += " AND ',' || dias || ',' LIKE ?"
        params.append(f"%,{dia},%")
    if bairro:
        sql += " AND lower(bairro) LIKE ?"
        params.append(f"%{bairro.strip().lower()}%")
    if tipo:
        sql += " AND tipo = ?"
        params.append(tipo)
    if apenas_regulares:
        sql += " AND regular = 1"
    sql += " ORDER BY nome COLLATE NOCASE"
    if limite:
        sql += " LIMIT ?"
        params.append(int(limite))
    return [_linha(row) for row in conn.execute(sql, params)]


def buscar(conn: sqlite3.Connection, termo: str, limite: int = 10) -> list[FeiraRegistro]:
    alvo = f"%{termo.strip().lower()}%"
    rows = conn.execute(
        "SELECT * FROM feiras WHERE lower(nome) LIKE ? OR lower(bairro) LIKE ?"
        " OR lower(endereco) LIKE ? ORDER BY nome COLLATE NOCASE LIMIT ?",
        (alvo, alvo, alvo, limite),
    )
    return [_linha(row) for row in rows]


def obter(conn: sqlite3.Connection, slug_ou_id: str) -> FeiraRegistro | None:
    if str(slug_ou_id).isdigit():
        row = conn.execute("SELECT * FROM feiras WHERE id = ?", (int(slug_ou_id),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM feiras WHERE slug = ?", (slug_ou_id,)).fetchone()
    return _linha(row) if row else None


def evidencias(conn: sqlite3.Connection, feira_id: int) -> list[EvidenciaRegistro]:
    rows = conn.execute(
        "SELECT e.campo, f.nome, f.url, e.trecho, e.capturado_em, f.confiabilidade"
        " FROM feiras_evidencias e JOIN feiras_fontes f ON f.id = e.fonte_id"
        " WHERE e.feira_id = ? ORDER BY e.campo, f.nome",
        (feira_id,),
    )
    return [
        EvidenciaRegistro(
            campo=r["campo"], fonte=r["nome"], fonte_url=r["url"], trecho=r["trecho"],
            capturado_em=r["capturado_em"], confiabilidade=r["confiabilidade"],
        )
        for r in rows
    ]


def fontes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT chave, nome, organizacao, url, confiabilidade, acessibilidade,"
        " http_status, metodo_captura, snapshot_path, hash_snapshot, observacao"
        " FROM feiras_fontes ORDER BY chave"
    )
    return [dict(r) for r in rows]


def resumo(conn: sqlite3.Connection) -> dict[str, int]:
    total = conn.execute("SELECT count(*) FROM feiras").fetchone()[0]
    com_dia = conn.execute("SELECT count(*) FROM feiras WHERE dias <> ''").fetchone()[0]
    evid = conn.execute("SELECT count(*) FROM feiras_evidencias").fetchone()[0]
    return {
        "feiras": int(total),
        "com_dia_publicado": int(com_dia),
        "sem_dia_publicado": int(total) - int(com_dia),
        "evidencias": int(evid),
        "fontes": conn.execute("SELECT count(*) FROM feiras_fontes").fetchone()[0],
    }


__all__ = [
    "EvidenciaRegistro",
    "FeiraRegistro",
    "ResumoCarregamento",
    "buscar",
    "carregar",
    "evidencias",
    "fontes",
    "listar",
    "obter",
    "resumo",
]
