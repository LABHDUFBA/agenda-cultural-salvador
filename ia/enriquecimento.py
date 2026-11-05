"""Orquestração do enriquecimento por IA — opcional, orçado e best-effort.

Contrato com o resto do projeto:

* **desligado por padrão**: sem ``IaConfig.habilitada`` nada acontece, nem uma
  leitura de tabela;
* **parcimonioso**: só entra no lote o evento cuja extração determinística
  deixou lacuna (categoria vazia ou confiança baixa), com três tetos somados —
  por item (elegibilidade), por lote (``max_chamadas_lote``) e por dia
  (``max_chamadas_dia``, contado de ``ia_chamadas`` no banco);
* **sem custo repetido**: a mesma entrada (hash SHA-256) já classificada não
  gera nova chamada; o acerto vem do cache;
* **fail-safe**: qualquer erro (rede, HTTP, JSON, validação) vira contador no
  relatório e detalhe sanitizado. A coleta e o status do evento seguem
  exatamente como estavam;
* **sem aprovação**: a sugestão é gravada em ``ia_sugestoes`` e nada mais. Não
  existe aqui chamada a ``aprovar_evento`` nem escrita em ``eventos``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from collectors.models import EventoColetado

from .cliente import ClienteRefuseless, ErroIA, ErroRespostaIA, sanitizar
from .config import IaConfig
from .modelos import SugestaoIA, TextoEntrada, texto_de_evento
from .repository import (
    chamadas_no_dia,
    gravar_sugestao,
    registrar_chamada,
    sugestao_por_hash,
)

# Só vale gastar uma chamada quando a extração determinística ficou incompleta.
MOTIVOS_ELEGIBILIDADE = (
    "categoria_ausente",
    "confianca_baixa",
)


def motivo_elegibilidade(evento: EventoColetado) -> str:
    """Por que este evento merece (ou não) uma chamada de IA."""
    if not evento.categoria.strip():
        return "categoria_ausente"
    if evento.confianca == "baixa":
        return "confianca_baixa"
    return ""


def entrada_do_evento(evento: EventoColetado, evento_id: int | None = None) -> TextoEntrada:
    """Proveniência da entrada: campos já extraídos + trecho da fonte."""
    return texto_de_evento(
        titulo=evento.titulo,
        local=evento.local_nome,
        descricao=evento.descricao,
        trecho=evento.trecho,
        url_original=evento.url_original,
        categoria=evento.categoria,
        preco=evento.preco,
        evento_id=evento_id,
    )


@dataclass
class ResultadoEnriquecimento:
    sugeridas: int = 0
    ignoradas: int = 0
    em_cache: int = 0
    bloqueadas: int = 0
    falhas: int = 0
    chamadas: int = 0
    tokens_prompt: int = 0
    tokens_resposta: int = 0
    motivo_ultima_falha: str = ""
    detalhes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "habilitada": True,
            "sugeridas": self.sugeridas,
            "ignoradas": self.ignoradas,
            "em_cache": self.em_cache,
            "bloqueadas": self.bloqueadas,
            "falhas": self.falhas,
            "chamadas": self.chamadas,
            "tokens_prompt": self.tokens_prompt,
            "tokens_resposta": self.tokens_resposta,
            "motivo_ultima_falha": self.motivo_ultima_falha,
        }


class EnriquecimentoIA:
    """Classificação assistida para a fila de revisão. Nunca decide nada."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: IaConfig,
        *,
        cliente: ClienteRefuseless | None = None,
    ) -> None:
        self.conn = conn
        self.config = config
        self._cliente = cliente

    # -- infraestrutura ------------------------------------------------------
    @property
    def cliente(self) -> ClienteRefuseless:
        if self._cliente is None:
            self._cliente = ClienteRefuseless(self.config)
        return self._cliente

    def _registrar(self, *, modelo: str, hash_entrada: str, resultado: str,
                   evento_id: int | None, detalhe: str = "", duracao_ms: int = 0) -> None:
        """Grava a tentativa; se o banco recusar, não interrompe o lote."""
        try:
            registrar_chamada(
                self.conn,
                modelo=modelo,
                hash_entrada=hash_entrada,
                resultado=resultado,
                evento_id=evento_id,
                detalhe=detalhe,
                duracao_ms=duracao_ms,
            )
        except sqlite3.Error:
            pass

    def restante_no_dia(self) -> int:
        try:
            usadas = chamadas_no_dia(self.conn)
        except sqlite3.Error:
            return 0
        return max(0, self.config.max_chamadas_dia - usadas)

    # -- lote ----------------------------------------------------------------
    def processar_lote(
        self, pares: Iterable[tuple[EventoColetado, int | None]]
    ) -> ResultadoEnriquecimento:
        """Classifica os elegíveis do lote dentro dos três tetos de uso."""
        resultado = ResultadoEnriquecimento()
        if not self.config.habilitada:
            # Desligada: nem lê o banco. O relatório diz que não houve trabalho.
            resultado.detalhes.append({"habilitada": False, "motivo": self.config.motivo_desabilitada})
            return resultado

        restante_dia = self.restante_no_dia()
        for evento, evento_id in pares:
            motivo = motivo_elegibilidade(evento)
            if not motivo:
                resultado.ignoradas += 1
                continue
            if resultado.chamadas >= self.config.max_chamadas_lote:
                resultado.bloqueadas += 1
                continue
            if restante_dia <= 0:
                resultado.bloqueadas += 1
                entrada = entrada_do_evento(evento, evento_id)
                self._registrar(
                    modelo=self.config.modelo,
                    hash_entrada=entrada.hash_entrada,
                    resultado="limite",
                    evento_id=evento_id,
                    detalhe="teto diário atingido",
                )
                continue

            entrada = entrada_do_evento(evento, evento_id)
            cache = None
            try:
                cache = sugestao_por_hash(self.conn, entrada.hash_entrada)
            except sqlite3.Error:
                cache = None
            if cache is not None:
                resultado.em_cache += 1
                self._registrar(
                    modelo=str(cache.get("modelo") or self.config.modelo),
                    hash_entrada=entrada.hash_entrada,
                    resultado="cache",
                    evento_id=evento_id,
                    detalhe=f"sugestao={cache.get('id')}",
                )
                continue

            try:
                sugestao, resposta = self.cliente.classificar(entrada)
            except ErroIA as erro:
                detalhe = sanitizar(str(erro), self.config.api_key)
                resultado.falhas += 1
                resultado.chamadas += 1
                resultado.motivo_ultima_falha = detalhe
                self._registrar(
                    modelo=self.config.modelo,
                    hash_entrada=entrada.hash_entrada,
                    resultado="erro",
                    evento_id=evento_id,
                    detalhe=detalhe,
                    duracao_ms=getattr(erro, "duracao_ms", 0),
                )
                continue
            except Exception as erro:  # noqa: BLE001 - IA nunca derruba a coleta
                detalhe = sanitizar(f"{type(erro).__name__}: {erro}", self.config.api_key)
                resultado.falhas += 1
                resultado.chamadas += 1
                resultado.motivo_ultima_falha = detalhe
                self._registrar(
                    modelo=self.config.modelo,
                    hash_entrada=entrada.hash_entrada,
                    resultado="erro",
                    evento_id=evento_id,
                    detalhe=detalhe,
                    duracao_ms=getattr(erro, "duracao_ms", 0),
                )
                continue

            resultado.chamadas += 1
            resultado.tokens_prompt += resposta.tokens_prompt
            resultado.tokens_resposta += resposta.tokens_resposta
            restante_dia -= 1
            try:
                sugestao_id = gravar_sugestao(
                    self.conn,
                    sugestao,
                    evento_id=evento_id,
                    base_url=self.config.base_url,
                )
            except (sqlite3.Error, ValueError) as erro:
                resultado.falhas += 1
                resultado.motivo_ultima_falha = sanitizar(str(erro), self.config.api_key)
                self._registrar(
                    modelo=resposta.modelo,
                    hash_entrada=entrada.hash_entrada,
                    resultado="ok",
                    evento_id=evento_id,
                    detalhe=f"falha ao gravar: {type(erro).__name__}",
                    duracao_ms=resposta.latencia_ms,
                )
                continue

            resultado.sugeridas += 1
            self._registrar(
                modelo=resposta.modelo,
                hash_entrada=entrada.hash_entrada,
                resultado="ok",
                evento_id=evento_id,
                detalhe=f"sugestao={sugestao_id}",
                duracao_ms=resposta.latencia_ms,
            )
            resultado.detalhes.append(
                {
                    "evento_id": evento_id,
                    "sugestao_id": sugestao_id,
                    "motivo": motivo,
                    "categoria": sugestao.categoria,
                    "confianca": sugestao.confianca,
                    "revisao_obrigatoria": sugestao.revisao_obrigatoria,
                }
            )
        return resultado

    # -- uma entrada avulsa --------------------------------------------------
    def sugerir(self, entrada: TextoEntrada) -> SugestaoIA:
        """Chamada única (CLI de verificação). Levanta ``ErroIA`` se falhar."""
        sugestao, _ = self.cliente.classificar(entrada)
        return sugestao


def pares_de_ingestao(
    itens: Sequence[EventoColetado], eventos_ids: Sequence[int | None]
) -> list[tuple[EventoColetado, int | None]]:
    """Emparelha cada evento coletado com o id canônico (ou None se rejeitado)."""
    return list(zip(itens, eventos_ids))


__all__ = [
    "EnriquecimentoIA",
    "ErroIA",
    "ErroRespostaIA",
    "MOTIVOS_ELEGIBILIDADE",
    "ResultadoEnriquecimento",
    "entrada_do_evento",
    "motivo_elegibilidade",
    "pares_de_ingestao",
]
