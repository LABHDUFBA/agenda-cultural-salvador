"""Núcleo de dados da agenda cultural de Salvador.

Uso típico:

    from database import abrir_banco, Fonte, Evento, Proveniencia
    from database import registrar_fonte, iniciar_sessao, ingerir_evento

    conn = abrir_banco("agenda.db")
    fonte_id = registrar_fonte(conn, Fonte(nome="Portal X", url_base="https://x.com"))
    sessao_id = iniciar_sessao(conn, fonte_id)
    resultado = ingerir_evento(conn, evento, proveniencia)

Interfaces detalhadas em database/README.md.
"""

from .connection import SCHEMA_VERSION, abrir_banco, conectar, inicializar, transacao
from .dedup import (
    SIMILARIDADE_LIMIAR,
    buscar_similar,
    chave_deduplicacao,
    data_ref_local,
    normalizar_texto,
    similaridade,
)
from .erros import DadoInvalido, ErroBanco, TransicaoInvalida
from .models import (
    Evento,
    Fonte,
    Proveniencia,
    Sessao,
    agora_utc,
    para_dt,
    para_iso,
)
from .repository import (
    ResultadoIngestao,
    aprovar_evento,
    finalizar_sessao,
    ingerir_evento,
    iniciar_sessao,
    listar_auditoria,
    listar_pendentes,
    obter_evento,
    registrar_auditoria,
    registrar_fonte,
    rejeitar_evento,
    requisitos_publicacao_evento,
)
from .validation import Decisao, requisitos_publicacao, validar_evento

__all__ = [
    "SCHEMA_VERSION",
    "abrir_banco",
    "conectar",
    "inicializar",
    "transacao",
    "SIMILARIDADE_LIMIAR",
    "buscar_similar",
    "chave_deduplicacao",
    "data_ref_local",
    "normalizar_texto",
    "similaridade",
    "DadoInvalido",
    "ErroBanco",
    "TransicaoInvalida",
    "Evento",
    "Fonte",
    "Proveniencia",
    "Sessao",
    "agora_utc",
    "para_dt",
    "para_iso",
    "ResultadoIngestao",
    "aprovar_evento",
    "finalizar_sessao",
    "ingerir_evento",
    "iniciar_sessao",
    "listar_auditoria",
    "listar_pendentes",
    "obter_evento",
    "registrar_auditoria",
    "registrar_fonte",
    "rejeitar_evento",
    "requisitos_publicacao_evento",
    "Decisao",
    "requisitos_publicacao",
    "validar_evento",
]
