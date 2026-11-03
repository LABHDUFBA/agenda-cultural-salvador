"""Repositório: escrita e leitura do núcleo de dados.

Toda ingestão é transacional e deixa rastro em `auditoria` e
`proveniencia`. O status inicial de um evento aceito é sempre `pendente`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass

from .connection import transacao
from .dedup import (
    buscar_similar,
    chave_deduplicacao,
    data_ref_local,
    normalizar_texto,
)
from .erros import DadoInvalido, TransicaoInvalida
from .models import (
    Evento,
    Fonte,
    Proveniencia,
    agora_utc,
    para_iso,
)
from .validation import (
    STATUS_PENDENTE,
    STATUS_REJEITADO,
    requisitos_publicacao,
    validar_evento,
)

STATUS_APROVADO = "aprovado"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ResultadoIngestao:
    status: str  # pendente | rejeitado | duplicado
    evento_id: int | None
    motivos: tuple[str, ...] = ()
    chave_dedup: str | None = None
    duplicata_de: int | None = None


def registrar_auditoria(
    conn: sqlite3.Connection,
    *,
    entidade: str,
    entidade_id: int | None,
    acao: str,
    ator: str = "sistema",
    estado_anterior: str | None = None,
    estado_novo: str | None = None,
    detalhe: str = "",
) -> None:
    conn.execute(
        "INSERT INTO auditoria "
        "(entidade, entidade_id, acao, estado_anterior, estado_novo, ator, detalhe, ocorrido_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entidade,
            entidade_id,
            acao,
            estado_anterior,
            estado_novo,
            ator,
            detalhe,
            para_iso(agora_utc()),
        ),
    )


def registrar_fonte(conn: sqlite3.Connection, fonte: Fonte) -> int:
    if not fonte.nome.strip():
        raise DadoInvalido("nome da fonte vazio")
    if not fonte.url_base.lower().startswith(("http://", "https://")):
        raise DadoInvalido("url_base deve ser http(s)")
    if fonte.confiabilidade not in ("alta", "media", "baixa"):
        raise DadoInvalido("confiabilidade deve ser alta, media ou baixa")
    try:
        with transacao(conn):
            cur = conn.execute(
                "INSERT INTO fontes (nome, url_base, tipo, confiabilidade, criado_em) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    fonte.nome.strip(),
                    fonte.url_base.strip(),
                    fonte.tipo,
                    fonte.confiabilidade,
                    para_iso(agora_utc()),
                ),
            )
            fonte_id = int(cur.lastrowid)
            registrar_auditoria(
                conn,
                entidade="fonte",
                entidade_id=fonte_id,
                acao="criada",
                estado_novo=fonte.nome.strip(),
            )
    except sqlite3.IntegrityError as exc:
        raise DadoInvalido(f"fonte já cadastrada: {fonte.nome}") from exc
    return fonte_id


def iniciar_sessao(
    conn: sqlite3.Connection,
    fonte_id: int,
    *,
    iniciada_em=None,
) -> int:
    if conn.execute("SELECT 1 FROM fontes WHERE id = ?", (fonte_id,)).fetchone() is None:
        raise DadoInvalido(f"fonte inexistente: {fonte_id}")
    iniciada = iniciada_em or agora_utc()
    with transacao(conn):
        cur = conn.execute(
            "INSERT INTO sessoes (fonte_id, iniciada_em, status) VALUES (?, ?, 'aberta')",
            (fonte_id, para_iso(iniciada)),
        )
        sessao_id = int(cur.lastrowid)
        registrar_auditoria(
            conn,
            entidade="sessao",
            entidade_id=sessao_id,
            acao="aberta",
            estado_novo="aberta",
            detalhe=f"fonte_id={fonte_id}",
        )
    return sessao_id


def finalizar_sessao(
    conn: sqlite3.Connection,
    sessao_id: int,
    *,
    status: str = "concluida",
    erro: str | None = None,
) -> None:
    if status not in ("concluida", "falha"):
        raise DadoInvalido("status final deve ser concluida ou falha")
    with transacao(conn):
        linha = conn.execute(
            "SELECT status FROM sessoes WHERE id = ?", (sessao_id,)
        ).fetchone()
        if linha is None:
            raise DadoInvalido(f"sessão inexistente: {sessao_id}")
        if linha["status"] != "aberta":
            raise TransicaoInvalida(f"sessão {sessao_id} já finalizada")
        conn.execute(
            "UPDATE sessoes SET status = ?, finalizada_em = ?, erro = ? WHERE id = ?",
            (status, para_iso(agora_utc()), erro, sessao_id),
        )
        registrar_auditoria(
            conn,
            entidade="sessao",
            entidade_id=sessao_id,
            acao="finalizada",
            estado_anterior="aberta",
            estado_novo=status,
            detalhe=erro or "",
        )


def _contar(conn: sqlite3.Connection, sessao_id: int, campo: str) -> None:
    if campo not in ("coletados", "aceitos", "rejeitados", "duplicados"):
        raise DadoInvalido(f"contador inválido: {campo}")
    cur = conn.execute(
        f"UPDATE sessoes SET {campo} = {campo} + 1 WHERE id = ? AND status = 'aberta'",
        (sessao_id,),
    )
    if cur.rowcount != 1:
        raise DadoInvalido(f"sessão {sessao_id} não está aberta")


def _hash_padrao(evento: Evento) -> str:
    base = "|".join(
        [
            evento.titulo.strip(),
            str(evento.inicio_em),
            evento.local_nome.strip(),
            evento.municipio.strip(),
        ]
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _validar_proveniencia(
    conn: sqlite3.Connection, prov: Proveniencia
) -> tuple[str, str]:
    url = (prov.url_original or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise DadoInvalido("url_original deve ser http(s)")
    if conn.execute("SELECT 1 FROM fontes WHERE id = ?", (prov.fonte_id,)).fetchone() is None:
        raise DadoInvalido(f"fonte inexistente: {prov.fonte_id}")
    if prov.sessao_id is not None:
        sessao = conn.execute(
            "SELECT fonte_id, status FROM sessoes WHERE id = ?", (prov.sessao_id,)
        ).fetchone()
        if sessao is None:
            raise DadoInvalido(f"sessão inexistente: {prov.sessao_id}")
        if sessao["fonte_id"] != prov.fonte_id:
            raise DadoInvalido("sessão não pertence à fonte informada")
        if sessao["status"] != "aberta":
            raise DadoInvalido(f"sessão {prov.sessao_id} não está aberta")
    hash_conteudo = (prov.hash_conteudo or "").strip().lower()
    if not hash_conteudo:
        raise DadoInvalido("hash_conteudo obrigatório")
    if not _HEX64.match(hash_conteudo):
        raise DadoInvalido("hash_conteudo deve ser sha256 hexadecimal de 64 caracteres")
    capturado = para_iso(prov.capturado_em or agora_utc())
    return url, capturado


def _inserir_evento(
    conn: sqlite3.Connection,
    evento: Evento,
    *,
    status: str,
    motivo: str,
    chave: str | None,
    canonico_id: int | None = None,
    url_fonte: str = "",
) -> int:
    inicio_iso = para_iso(evento.inicio_em)
    fim_iso = para_iso(evento.fim_em) if evento.fim_em is not None else None
    agora = para_iso(agora_utc())
    cur = conn.execute(
        "INSERT INTO eventos ("
        "titulo, titulo_normalizado, descricao, inicio_em, fim_em, data_ref, "
        "local_nome, local_normalizado, endereco, bairro, municipio, uf, "
        "latitude, longitude, categoria, url_ingresso, preco, status, motivo, "
        "chave_dedup, evento_canonico_id, criado_em, atualizado_em, url_fonte"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            evento.titulo.strip(),
            normalizar_texto(evento.titulo),
            evento.descricao,
            inicio_iso,
            fim_iso,
            data_ref_local(inicio_iso),
            evento.local_nome.strip(),
            normalizar_texto(evento.local_nome),
            evento.endereco,
            evento.bairro,
            evento.municipio.strip(),
            (evento.uf or "").strip().upper(),
            evento.latitude,
            evento.longitude,
            evento.categoria,
            evento.url_ingresso,
            evento.preco,
            status,
            motivo,
            chave,
            canonico_id,
            agora,
            agora,
            url_fonte.strip(),
        ),
    )
    return int(cur.lastrowid)


def _inserir_proveniencia(
    conn: sqlite3.Connection,
    evento_id: int,
    prov: Proveniencia,
    *,
    url: str,
    capturado_em: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO proveniencia "
        "(evento_id, fonte_id, sessao_id, url_original, capturado_em, hash_conteudo, trecho) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            evento_id,
            prov.fonte_id,
            prov.sessao_id,
            url,
            capturado_em,
            prov.hash_conteudo.strip().lower(),
            prov.trecho,
        ),
    )


def ingerir_evento(
    conn: sqlite3.Connection,
    evento: Evento,
    prov: Proveniencia,
    *,
    agora=None,
    motivos_extra: tuple[str, ...] | list[str] = (),
) -> ResultadoIngestao:
    """Valida, deduplica e grava um evento com proveniência e auditoria.

    `motivos_extra` registra metainformação de extração (ex.: confiança baixa,
    campos inferidos). Nunca aprova: só acompanha o motivo do evento pendente.
    """
    url, capturado = _validar_proveniencia(conn, prov)
    if prov.sessao_id is None:
        raise DadoInvalido("sessao_id obrigatório na ingestão")
    decisao = validar_evento(evento, agora=agora)

    with transacao(conn):
        _contar(conn, prov.sessao_id, "coletados")

        if decisao.rejeitado:
            motivo = ";".join(decisao.motivos)
            evento_id: int | None = None
            try:
                evento_id = _inserir_evento(
                    conn,
                    evento,
                    status=STATUS_REJEITADO,
                    motivo=motivo,
                    chave=None,
                    url_fonte=url,
                )
            except (ValueError, sqlite3.IntegrityError):
                # Data que não dá para persistir: a recusa fica só na auditoria.
                evento_id = None
            detalhe = json.dumps(
                {
                    "url": url,
                    "titulo": evento.titulo,
                    "inicio_em": str(evento.inicio_em),
                    "motivos": list(decisao.motivos),
                    "evidencias": list(decisao.evidencias),
                },
                ensure_ascii=False,
            )
            if evento_id is not None:
                _inserir_proveniencia(
                    conn, evento_id, prov, url=url, capturado_em=capturado
                )
            registrar_auditoria(
                conn,
                entidade="evento",
                entidade_id=evento_id,
                acao="ingestao_rejeitada",
                estado_novo=STATUS_REJEITADO,
                detalhe=detalhe,
            )
            _contar(conn, prov.sessao_id, "rejeitados")
            return ResultadoIngestao(
                status=STATUS_REJEITADO,
                evento_id=evento_id,
                motivos=decisao.motivos,
            )

        chave = chave_deduplicacao(
            titulo=evento.titulo,
            inicio_em=evento.inicio_em,
            local_nome=evento.local_nome,
            municipio=evento.municipio or "Salvador",
        )
        existente = conn.execute(
            "SELECT id FROM eventos WHERE chave_dedup = ?", (chave,)
        ).fetchone()
        if existente is not None:
            duplicata_de = int(existente["id"])
            _inserir_proveniencia(
                conn, duplicata_de, prov, url=url, capturado_em=capturado
            )
            registrar_auditoria(
                conn,
                entidade="evento",
                entidade_id=duplicata_de,
                acao="duplicado",
                detalhe=json.dumps(
                    {"url": url, "chave_dedup": chave, "titulo": evento.titulo},
                    ensure_ascii=False,
                ),
            )
            _contar(conn, prov.sessao_id, "duplicados")
            return ResultadoIngestao(
                status="duplicado",
                evento_id=duplicata_de,
                chave_dedup=chave,
                duplicata_de=duplicata_de,
            )

        similar = buscar_similar(
            conn,
            titulo=evento.titulo,
            data_ref=data_ref_local(evento.inicio_em),
        )
        motivos = list(decisao.motivos) + [
            str(m).strip() for m in motivos_extra if str(m).strip()
        ]
        if similar is not None:
            motivos.append(f"possivel_duplicado:{similar}")

        evento_id = _inserir_evento(
            conn,
            evento,
            status=STATUS_PENDENTE,
            motivo=";".join(motivos),
            chave=chave,
            url_fonte=url,
        )
        _inserir_proveniencia(conn, evento_id, prov, url=url, capturado_em=capturado)
        registrar_auditoria(
            conn,
            entidade="evento",
            entidade_id=evento_id,
            acao="ingestao",
            estado_novo=STATUS_PENDENTE,
            detalhe=json.dumps(
                {
                    "url": url,
                    "chave_dedup": chave,
                    "motivos": motivos,
                    "evidencias": list(decisao.evidencias),
                },
                ensure_ascii=False,
            ),
        )
        _contar(conn, prov.sessao_id, "aceitos")
        return ResultadoIngestao(
            status=STATUS_PENDENTE,
            evento_id=evento_id,
            motivos=tuple(motivos),
            chave_dedup=chave,
        )


def _evento_para_transicao(conn: sqlite3.Connection, evento_id: int) -> sqlite3.Row:
    linha = conn.execute(
        "SELECT id, status FROM eventos WHERE id = ?", (evento_id,)
    ).fetchone()
    if linha is None:
        raise DadoInvalido(f"evento inexistente: {evento_id}")
    return linha


def aprovar_evento(
    conn: sqlite3.Connection,
    evento_id: int,
    *,
    ator: str,
    motivo: str = "",
) -> None:
    if not ator or not ator.strip():
        raise DadoInvalido("ator obrigatório")
    with transacao(conn):
        linha = _evento_para_transicao(conn, evento_id)
        if linha["status"] != STATUS_PENDENTE:
            raise TransicaoInvalida(
                f"evento {evento_id} está '{linha['status']}', não 'pendente'"
            )
        conn.execute(
            "UPDATE eventos SET status = ?, atualizado_em = ? WHERE id = ?",
            (STATUS_APROVADO, para_iso(agora_utc()), evento_id),
        )
        registrar_auditoria(
            conn,
            entidade="evento",
            entidade_id=evento_id,
            acao="aprovado",
            ator=ator.strip(),
            estado_anterior=STATUS_PENDENTE,
            estado_novo=STATUS_APROVADO,
            detalhe=motivo,
        )


def rejeitar_evento(
    conn: sqlite3.Connection,
    evento_id: int,
    *,
    ator: str,
    motivo: str,
) -> None:
    if not ator or not ator.strip():
        raise DadoInvalido("ator obrigatório")
    if not motivo or not motivo.strip():
        raise DadoInvalido("motivo obrigatório para rejeitar")
    with transacao(conn):
        linha = _evento_para_transicao(conn, evento_id)
        if linha["status"] != STATUS_PENDENTE:
            raise TransicaoInvalida(
                f"evento {evento_id} está '{linha['status']}', não 'pendente'"
            )
        conn.execute(
            "UPDATE eventos SET status = ?, motivo = ?, atualizado_em = ? WHERE id = ?",
            (STATUS_REJEITADO, motivo.strip(), para_iso(agora_utc()), evento_id),
        )
        registrar_auditoria(
            conn,
            entidade="evento",
            entidade_id=evento_id,
            acao="rejeitado",
            ator=ator.strip(),
            estado_anterior=STATUS_PENDENTE,
            estado_novo=STATUS_REJEITADO,
            detalhe=motivo.strip(),
        )


def listar_pendentes(conn: sqlite3.Connection, *, limite: int = 100) -> list[dict]:
    if limite <= 0:
        raise DadoInvalido("limite deve ser positivo")
    linhas = conn.execute(
        "SELECT * FROM eventos WHERE status = 'pendente' "
        "ORDER BY inicio_em ASC, id ASC LIMIT ?",
        (limite,),
    ).fetchall()
    return [dict(linha) for linha in linhas]


def obter_evento(conn: sqlite3.Connection, evento_id: int) -> dict | None:
    linha = conn.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,)).fetchone()
    if linha is None:
        return None
    evento = dict(linha)
    evento["proveniencia"] = [
        dict(p)
        for p in conn.execute(
            "SELECT * FROM proveniencia WHERE evento_id = ? ORDER BY id", (evento_id,)
        )
    ]
    return evento


def requisitos_publicacao_evento(
    conn: sqlite3.Connection, evento_id: int
) -> tuple[str, ...]:
    """Evidências canônicas que faltam para publicar este evento.

    Somente leitura. Junta proveniência + confiabilidade da fonte e delega a
    regra para :func:`database.validation.requisitos_publicacao`.
    """
    linha = conn.execute(
        "SELECT * FROM eventos WHERE id = ?", (evento_id,)
    ).fetchone()
    if linha is None:
        return ("evento_inexistente",)
    proveniencias = [
        dict(p)
        for p in conn.execute(
            "SELECT p.url_original, p.hash_conteudo, p.trecho, "
            "f.confiabilidade AS fonte_confiabilidade "
            "FROM proveniencia p JOIN fontes f ON f.id = p.fonte_id "
            "WHERE p.evento_id = ?",
            (evento_id,),
        )
    ]
    return requisitos_publicacao(dict(linha), proveniencias)


def listar_auditoria(
    conn: sqlite3.Connection,
    *,
    entidade: str | None = None,
    entidade_id: int | None = None,
) -> list[dict]:
    sql = "SELECT * FROM auditoria WHERE 1 = 1"
    params: list[object] = []
    if entidade is not None:
        sql += " AND entidade = ?"
        params.append(entidade)
    if entidade_id is not None:
        sql += " AND entidade_id = ?"
        params.append(entidade_id)
    sql += " ORDER BY id"
    return [dict(linha) for linha in conn.execute(sql, params)]
