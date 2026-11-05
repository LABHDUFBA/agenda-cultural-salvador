"""Ponte entre a coleta e o núcleo de dados canônico.

O coletor continua sem gravar nada: este módulo é a **única** camada que
traduz ``EventoColetado`` em linhas de ``eventos``/``proveniencia``, sempre
através de :func:`database.ingerir_evento`.

Travas aplicadas aqui:

* só fonte do catálogo com ``acessibilidade == 'ok'`` e estrutura suportada é
  ingerida (fonte bloqueada/não suportada levanta ``FonteNaoColetavel``);
* nada nasce aprovado: ``ingerir_evento`` grava ``pendente`` ou ``rejeitado``;
* confiança baixa e campos inferidos viram motivos de revisão visíveis para o
  moderador (nunca justificam aprovação automática);
* falha de um item não apaga os anteriores: o erro fica no resumo e a sessão
  é finalizada como ``falha`` com o texto do problema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Iterable

from database import (
    DadoInvalido,
    Evento,
    Fonte,
    Proveniencia,
    ResultadoIngestao,
    finalizar_sessao,
    ingerir_evento,
    iniciar_sessao,
    listar_pendentes,
    obter_evento,
    registrar_fonte,
    requisitos_publicacao_evento,
)
from database.models import agora_utc
from sources.catalog import FonteCultural, por_id

from .models import EventoColetado
from .registry import obter_por_id

# Enriquecimento por IA é opcional e injetado: este módulo não importa ``ia``
# no topo para que o caminho determinístico continue sem depender dele.
if TYPE_CHECKING:  # pragma: no cover
    from ia.enriquecimento import EnriquecimentoIA


class FonteNaoColetavel(DadoInvalido):
    """A fonte existe no catálogo mas não pode ser coletada com segurança."""


@dataclass(frozen=True)
class ResultadoColeta:
    fonte: str
    fonte_id: int | None = None
    sessao_id: int | None = None
    coletados: int = 0
    aceitos: int = 0
    rejeitados: int = 0
    duplicados: int = 0
    resultados: tuple[ResultadoIngestao, ...] = ()
    erros: tuple[str, ...] = ()
    ia: dict | None = None

    @property
    def pendentes(self) -> int:
        return self.aceitos

    def to_dict(self) -> dict[str, object]:
        dados: dict[str, object] = {
            "fonte": self.fonte,
            "fonte_id": self.fonte_id,
            "sessao_id": self.sessao_id,
            "coletados": self.coletados,
            "aceitos": self.aceitos,
            "rejeitados": self.rejeitados,
            "duplicados": self.duplicados,
            "erros": list(self.erros),
        }
        if self.ia is not None:
            dados["ia"] = self.ia
        return dados


def garantir_fonte(conn, fonte: FonteCultural) -> int:
    """Registra a fonte do catálogo uma única vez e devolve o id canônico."""
    linha = conn.execute(
        "SELECT id FROM fontes WHERE nome = ?", (fonte.nome,)
    ).fetchone()
    if linha is not None:
        return int(linha["id"])
    return registrar_fonte(
        conn,
        Fonte(
            nome=fonte.nome,
            url_base=fonte.url,
            tipo="site",
            confiabilidade=fonte.confiabilidade,
        ),
    )


def motivos_de_revisao(evento: EventoColetado) -> tuple[str, ...]:
    """Metainformação honesta sobre o que a extração teve de assumir."""
    motivos: list[str] = []
    if evento.confianca == "baixa":
        motivos.append("revisao_obrigatoria:confianca_baixa")
    elif evento.confianca == "media":
        motivos.append(f"revisao_recomendada:confianca_media:{evento.metodo_extracao}")
    if evento.campos_inferidos:
        motivos.append("campos_inferidos:" + ",".join(sorted(evento.campos_inferidos)))
    if not evento.local_nome.strip():
        motivos.append("local_ausente_na_fonte")
    return tuple(motivos)


def ingerir_coletados(
    conn,
    fonte: FonteCultural,
    eventos: Iterable[EventoColetado],
    *,
    capturado_em: datetime | None = None,
    ia: "EnriquecimentoIA | None" = None,
) -> ResultadoColeta:
    """Grava os eventos coletados como pendentes/rejeitados no banco canônico.

    ``ia`` é opcional e só roda depois que a escrita determinística terminou:
    uma falha de IA não muda um único status, e o lote já está persistido.
    """
    if not fonte.coletavel:
        raise FonteNaoColetavel(
            f"fonte {fonte.id!r} não é coletável "
            f"(acessibilidade={fonte.acessibilidade!r}, tipo={fonte.tipo_conteudo!r})"
        )
    itens = list(eventos)
    if not itens:
        # Coleta válida com zero resultados (ex.: calendário vazio) não é erro.
        return ResultadoColeta(fonte=fonte.id)

    fonte_id = garantir_fonte(conn, fonte)
    sessao_id = iniciar_sessao(conn, fonte_id)
    capturado = capturado_em or agora_utc()
    resultados: list[ResultadoIngestao] = []
    erros: list[str] = []
    pares: list[tuple[EventoColetado, int]] = []

    try:
        for item in itens:
            try:
                evento = Evento(**item.evento_kwargs())
                prov = Proveniencia(
                    **item.proveniencia_kwargs(
                        fonte_id=fonte_id, sessao_id=sessao_id, capturado_em=capturado
                    )
                )
                resultado = ingerir_evento(
                    conn,
                    evento,
                    prov,
                    motivos_extra=motivos_de_revisao(item),
                )
                resultados.append(resultado)
                # Só evento pendente e persistido recebe sugestão: rejeitado não
                # volta atrás por opinião de modelo.
                if resultado.status == "pendente" and resultado.evento_id:
                    pares.append((item, int(resultado.evento_id)))
            except (ValueError, DadoInvalido) as exc:
                erros.append(f"{item.titulo!r}: {exc}")
        finalizar_sessao(
            conn,
            sessao_id,
            status="falha" if erros and not resultados else "concluida",
            erro="; ".join(erros)[:500] or None,
        )
    except BaseException:
        # Nunca deixa sessão aberta se algo inesperado interromper o lote.
        try:
            finalizar_sessao(conn, sessao_id, status="falha", erro="interrompido")
        except DadoInvalido:
            pass
        raise

    resumo_ia: dict | None = None
    if ia is not None:
        # Best-effort: o lote já está gravado e nenhum status depende disto.
        try:
            resumo_ia = ia.processar_lote(pares).to_dict()
        except Exception as exc:  # noqa: BLE001 - IA opcional nunca derruba coleta
            resumo_ia = {"habilitada": True, "erro": f"{type(exc).__name__}: {exc}"}

    contadores = conn.execute(
        "SELECT coletados, aceitos, rejeitados, duplicados FROM sessoes WHERE id = ?",
        (sessao_id,),
    ).fetchone()
    return ResultadoColeta(
        fonte=fonte.id,
        fonte_id=fonte_id,
        sessao_id=sessao_id,
        coletados=int(contadores["coletados"]),
        aceitos=int(contadores["aceitos"]),
        rejeitados=int(contadores["rejeitados"]),
        duplicados=int(contadores["duplicados"]),
        resultados=tuple(resultados),
        erros=tuple(erros),
        ia=resumo_ia,
    )


def coletar_e_ingerir(
    conn,
    fonte_id: str,
    *,
    limite: int | None = None,
    fetcher=None,
    desde: datetime | None = None,
    ate: datetime | None = None,
) -> ResultadoColeta:
    """Coleta a fonte do catálogo e persiste o resultado no banco canônico."""
    fonte = por_id(fonte_id)
    coletor = obter_por_id(fonte_id, fetcher)
    eventos = coletor.coletar(limite=limite, desde=desde, ate=ate)
    return ingerir_coletados(conn, fonte, eventos)


def fila_de_revisao(conn, limite: int = 50) -> list[dict]:
    """Eventos pendentes com o que falta para publicação (somente leitura)."""
    fila = listar_pendentes(conn, limite=limite)
    for evento in fila:
        evento["pendencias_publicacao"] = list(
            requisitos_publicacao_evento(conn, int(evento["id"]))
        )
        evento["proveniencia"] = obter_evento(conn, int(evento["id"]))["proveniencia"]
        # Sugestões de IA, quando existirem. Consultivas: quem lê decide.
        evento["sugestoes_ia"] = _sugestoes_ia(conn, int(evento["id"]))
    return fila


def _sugestoes_ia(conn, evento_id: int) -> list[dict]:
    """Leitura tolerante: banco antigo sem as tabelas de IA devolve lista vazia."""
    try:
        from ia.repository import sugestoes_do_evento  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return []
    try:
        return sugestoes_do_evento(conn, evento_id)
    except Exception:  # noqa: BLE001 - tabela ausente / banco somente leitura
        return []


__all__ = [
    "FonteNaoColetavel",
    "ResultadoColeta",
    "coletar_e_ingerir",
    "fila_de_revisao",
    "garantir_fonte",
    "ingerir_coletados",
    "motivos_de_revisao",
]
