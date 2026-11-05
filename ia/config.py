"""Configuração da integração **opcional** de IA (Refuseless).

Desligada por padrão. Ligar exige **duas** condições independentes:

* ``AGENDA_IA_ENABLED=1`` no ambiente — intenção explícita do operador;
* ``REFUSELESS_API_KEY`` não vazia — credencial presente.

Faltando qualquer uma, ``habilitada`` é ``False`` e nenhum código de rede é
executado. O caminho padrão do projeto (coletores determinísticos) continua
sendo o único que roda sem essa configuração.

Regra de segredo: a chave entra no objeto, é usada no cabeçalho HTTP e **nunca**
aparece em ``repr()``, em log, em mensagem de erro ou em saída de CLI. Este
módulo não lê arquivo ``.env``: o ambiente é responsabilidade do operador
(``.env`` é carregado pelo shell/container, nunca pelo processo).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional
from urllib.parse import urlparse

BASE_PADRAO = "https://api.refuseless.com/v1"
MODELO_PADRAO = "deepseek-v4.1-flash-abliterated"
VARIAVEL_CHAVE = "REFUSELESS_API_KEY"

_TIMEOUT_MIN, _TIMEOUT_MAX = 1.0, 120.0
_LOTE_MIN, _LOTE_MAX = 1, 10
_DIA_MIN, _DIA_MAX = 1, 500


def _flag(env: Mapping[str, str], nome: str) -> bool:
    return (env.get(nome) or "").strip().lower() in ("1", "true", "yes", "sim")


def _inteiro(env: Mapping[str, str], nome: str, padrao: int) -> int:
    bruto = (env.get(nome) or "").strip()
    if not bruto:
        return padrao
    try:
        return int(bruto)
    except ValueError:
        return padrao


def _decimal(env: Mapping[str, str], nome: str, padrao: float) -> float:
    bruto = (env.get(nome) or "").strip().replace(",", ".")
    if not bruto:
        return padrao
    try:
        return float(bruto)
    except ValueError:
        return padrao


def _base_valida(base: str) -> bool:
    """https:// sempre; http:// só para loopback explícito."""
    partes = urlparse(base)
    if partes.scheme == "https":
        return bool(partes.netloc)
    if partes.scheme == "http":
        return partes.hostname in ("127.0.0.1", "localhost", "::1")
    return False


@dataclass(frozen=True)
class IaConfig:
    """Parâmetros da integração. Imutável e segura por padrão."""

    habilitada: bool = False
    base_url: str = BASE_PADRAO
    modelo: str = MODELO_PADRAO
    # repr=False: a chave não vaza em log, print ou traceback do dataclass.
    api_key: str = field(default="", repr=False, compare=False)
    timeout_s: float = 20.0
    # Uso parcimonioso: teto por lote de coleta e teto por dia (UTC).
    max_chamadas_lote: int = 3
    max_chamadas_dia: int = 20
    temperatura: float = 0.0
    # Congelado na construção: por que está desligada. Sem segredo no texto.
    motivo: str = ""

    @property
    def sem_chave(self) -> bool:
        return not self.api_key.strip()

    @property
    def motivo_desabilitada(self) -> str:
        """Explicação curta e sem segredo do porquê está desligada."""
        return "" if self.habilitada else self.motivo

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def resumo(self) -> dict:
        """Retrato seguro para log/CLI. Nenhum segredo, nem mascarado."""
        return {
            "habilitada": self.habilitada,
            "base_url": self.base_url,
            "modelo": self.modelo,
            "chave_presente": not self.sem_chave,
            "timeout_s": self.timeout_s,
            "max_chamadas_lote": self.max_chamadas_lote,
            "max_chamadas_dia": self.max_chamadas_dia,
            "temperatura": self.temperatura,
            "motivo_desabilitada": self.motivo_desabilitada,
        }

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "IaConfig":
        env = os.environ if env is None else env
        base = (env.get("AGENDA_IA_BASE_URL") or "").strip() or BASE_PADRAO
        base = base.rstrip("/")
        if not _base_valida(base):
            raise ValueError(
                "AGENDA_IA_BASE_URL inválida: use https:// (http:// só para loopback)"
            )
        modelo = (env.get("AGENDA_IA_MODEL") or "").strip() or MODELO_PADRAO
        chave = (env.get(VARIAVEL_CHAVE) or "").strip()
        timeout = min(max(_decimal(env, "AGENDA_IA_TIMEOUT", 20.0), _TIMEOUT_MIN), _TIMEOUT_MAX)
        lote = min(max(_inteiro(env, "AGENDA_IA_MAX_CHAMADAS_LOTE", 3), _LOTE_MIN), _LOTE_MAX)
        dia = min(max(_inteiro(env, "AGENDA_IA_MAX_CHAMADAS_DIA", 20), _DIA_MIN), _DIA_MAX)
        temperatura = min(max(_decimal(env, "AGENDA_IA_TEMPERATURA", 0.0), 0.0), 1.0)
        # Habilitar exige a flag E a chave: uma variável esquecida não liga a IA.
        flag = _flag(env, "AGENDA_IA_ENABLED")
        habilitada = flag and bool(chave)
        faltando: list[str] = []
        if not flag:
            faltando.append("AGENDA_IA_ENABLED ausente")
        if not chave:
            faltando.append(f"{VARIAVEL_CHAVE} ausente")
        motivo = "" if habilitada else " e ".join(faltando)
        return cls(
            habilitada=habilitada,
            base_url=base,
            modelo=modelo,
            api_key=chave,
            timeout_s=timeout,
            max_chamadas_lote=lote,
            max_chamadas_dia=dia,
            temperatura=temperatura,
            motivo=motivo,
        )


__all__ = ["BASE_PADRAO", "IaConfig", "MODELO_PADRAO", "VARIAVEL_CHAVE"]
