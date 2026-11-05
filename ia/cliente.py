"""Cliente HTTP mínimo para a API compatível com OpenAI da Refuseless.

Só biblioteca padrão (``urllib``). O transporte é injetável para que os testes
rodem 100% offline: nenhum teste abre socket.

Convivência com o resto do projeto:

* falha aqui **nunca** derruba a coleta — o chamador trata toda exceção como
  "sem sugestão" e o evento segue como o coletor o entregou;
* a chave viaja só no cabeçalho ``Authorization``. Mensagens de erro passam por
  :func:`sanitizar`, que remove qualquer ocorrência da chave do texto;
* ``max_tokens`` é baixo de propósito: a resposta é um objeto JSON curto.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .config import IaConfig, VARIAVEL_CHAVE
from .esquema import SaidaInvalida, montar_mensagens, validar_saida
from .modelos import SugestaoIA, TextoEntrada

MAX_TOKENS_RESPOSTA = 480


class ErroIA(Exception):
    """Falha de integração. Texto já sanitizado (nunca contém a chave)."""


class ErroConfiguracaoIA(ErroIA):
    pass


class ErroTransporteIA(ErroIA):
    pass


class ErroRespostaIA(ErroIA):
    """Resposta recebida mas inutilizável. Guarda a latência para a auditoria."""

    def __init__(self, mensagem: str, *, duracao_ms: int = 0, finish_reason: str = "") -> None:
        super().__init__(mensagem)
        self.duracao_ms = max(0, int(duracao_ms))
        self.finish_reason = finish_reason or ""


@dataclass(frozen=True)
class RespostaIA:
    modelo: str
    latencia_ms: int
    tokens_prompt: int = 0
    tokens_resposta: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "modelo": self.modelo,
            "latencia_ms": self.latencia_ms,
            "tokens_prompt": self.tokens_prompt,
            "tokens_resposta": self.tokens_resposta,
        }


class Transporte(Protocol):
    def __call__(
        self, url: str, cabecalhos: dict[str, str], corpo: bytes, timeout: float
    ) -> tuple[int, bytes]: ...


def sanitizar(texto: str, segredo: str) -> str:
    """Remove o segredo de qualquer texto destinado a log/erro."""
    texto = str(texto or "")
    if segredo:
        texto = texto.replace(segredo, "***")
    # Chaves em formato ``sk-...``/``rf-...`` que possam ter vazado de outra origem.
    partes = []
    for pedaco in texto.split():
        if len(pedaco) >= 20 and any(pedaco.startswith(p) for p in ("sk-", "rf-", "key-")):
            pedaco = "***"
        partes.append(pedaco)
    return " ".join(partes)[:600]


def http_post_padrao(
    url: str, cabecalhos: dict[str, str], corpo: bytes, timeout: float
) -> tuple[int, bytes]:
    """POST real. HTTPError também é resposta: devolve status + corpo."""
    pedido = urllib.request.Request(url, data=corpo, headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as resposta:
            return int(resposta.status), resposta.read()
    except urllib.error.HTTPError as erro:
        return int(erro.code), erro.read() or b""
    except (urllib.error.URLError, socket.timeout) as erro:
        motivo = getattr(erro, "reason", erro)
        raise ErroTransporteIA(f"falha de rede: {motivo}") from erro


class ClienteRefuseless:
    """Uma chamada de chat/completions por classificação. Sem retry."""

    def __init__(
        self,
        config: IaConfig,
        *,
        transporte: Transporte | None = None,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        if config.sem_chave:
            raise ErroConfiguracaoIA(f"{VARIAVEL_CHAVE} ausente")
        self.config = config
        self._transporte = transporte or http_post_padrao
        self._relogio = relogio

    # -- corpo da requisição -------------------------------------------------
    def corpo(self, entrada: TextoEntrada) -> dict[str, Any]:
        return {
            "model": self.config.modelo,
            "messages": montar_mensagens(entrada),
            "temperature": self.config.temperatura,
            "max_tokens": MAX_TOKENS_RESPOSTA,
        }

    def cabecalhos(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "agenda-cultural-salvador/1.0 (ia opcional)",
        }

    # -- chamada -------------------------------------------------------------
    def classificar(self, entrada: TextoEntrada) -> tuple[SugestaoIA, RespostaIA]:
        """Envia o texto e devolve sugestão validada + metadados da resposta."""
        corpo = json.dumps(self.corpo(entrada), ensure_ascii=False).encode("utf-8")
        inicio = self._relogio()
        try:
            status, bruto = self._transporte(
                self.config.endpoint, self.cabecalhos(), corpo, self.config.timeout_s
            )
        except ErroIA as erro:
            raise ErroTransporteIA(sanitizar(str(erro), self.config.api_key)) from erro
        except (urllib.error.URLError, socket.timeout, OSError) as erro:
            motivo = getattr(erro, "reason", erro)
            raise ErroTransporteIA(
                sanitizar(f"falha de rede: {motivo}", self.config.api_key)
            ) from erro
        latencia = max(0, int((self._relogio() - inicio) * 1000))

        if status != 200:
            detalhe = sanitizar(bruto.decode("utf-8", "replace"), self.config.api_key)
            raise ErroRespostaIA(f"HTTP {status}: {detalhe[:300]}", duracao_ms=latencia)
        try:
            dados = json.loads(bruto.decode("utf-8", "replace"))
        except (UnicodeDecodeError, json.JSONDecodeError) as erro:
            raise ErroRespostaIA(
                f"JSON de resposta inválido: {erro}", duracao_ms=latencia
            ) from erro
        if not isinstance(dados, dict):
            raise ErroRespostaIA("resposta não é objeto JSON", duracao_ms=latencia)

        escolhas = dados.get("choices") or []
        if not escolhas or not isinstance(escolhas, list):
            raise ErroRespostaIA("resposta sem 'choices'", duracao_ms=latencia)
        primeira = escolhas[0] if isinstance(escolhas[0], dict) else {}
        motivo_fim = str(primeira.get("finish_reason") or "")
        mensagem = primeira.get("message")
        conteudo = (mensagem or {}).get("content") if isinstance(mensagem, dict) else None
        if not isinstance(conteudo, str) or not conteudo.strip():
            raise ErroRespostaIA(
                "resposta sem conteúdo de mensagem",
                duracao_ms=latencia,
                finish_reason=motivo_fim,
            )

        modelo = str(dados.get("model") or self.config.modelo)
        uso = dados.get("usage") or {}
        try:
            sugestao = validar_saida(conteudo, entrada, modelo=modelo)
        except SaidaInvalida as erro:
            # Diagnóstico honesto: motivo do corte + começo da saída, sanitizado.
            pista = sanitizar(conteudo.strip(), self.config.api_key)[:200]
            raise ErroRespostaIA(
                f"saída inválida ({motivo_fim or 'sem finish_reason'}): {erro}; "
                f"início={pista!r}",
                duracao_ms=latencia,
                finish_reason=motivo_fim,
            ) from erro
        resposta = RespostaIA(
            modelo=modelo,
            latencia_ms=latencia,
            tokens_prompt=int(uso.get("prompt_tokens") or 0),
            tokens_resposta=int(uso.get("completion_tokens") or 0),
        )
        return sugestao, resposta


__all__ = [
    "ClienteRefuseless",
    "ErroConfiguracaoIA",
    "ErroIA",
    "ErroRespostaIA",
    "ErroTransporteIA",
    "MAX_TOKENS_RESPOSTA",
    "RespostaIA",
    "Transporte",
    "http_post_padrao",
    "sanitizar",
]
