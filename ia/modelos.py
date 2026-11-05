"""Contratos de entrada e saída da integração de IA.

Nada aqui importa rede, banco ou Telegram: são estruturas puras, testáveis
em isolamento.

Princípios gravados no tipo, não só na documentação:

* :class:`TextoEntrada` carrega a **proveniência da entrada** (URL da fonte,
  trecho e hash SHA-256 do texto exato enviado). Sem isso não há chamada:
  uma sugestão sem entrada rastreável não é auditável.
* :class:`SugestaoIA` é uma **opinião**, não uma decisão. Ela não tem campo
  ``status``, não tem ``autorizacao_*`` e :attr:`SugestaoIA.pode_aprovar` é
  sempre ``False`` por construção — não existe caminho de código que a
  transforme em aprovação.
* A saída só pode tocar metadados de classificação. Campos que o portão de
  publicação usa (município, UF, local, data, fonte, descrição) estão fora do
  contrato: se o modelo os devolver, são descartados na validação.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

# Vocabulário fechado de categorias. Valor fora daqui não vira sugestão.
CATEGORIAS = (
    "musica",
    "teatro",
    "cinema",
    "danca",
    "artes_visuais",
    "literatura",
    "circo",
    "festa_popular",
    "oficina",
    "infantil",
    "gastronomia",
    "outro",
)

PUBLICOS = ("livre", "adulto", "infantil", "juvenil", "idoso", "lgbtqia", "misto")

FAIXAS = ("livre", "0-6", "7-12", "12+", "14+", "16+", "18+")

ACESSIBILIDADES = (
    "libras",
    "audiodescricao",
    "rampa",
    "legenda",
    "cadeirante",
    "braille",
    "nao_informado",
)

LIMITE_TEXTO = 1200
LIMITE_TRECHO = 500


def _limpar(valor: str, limite: int) -> str:
    texto = unicodedata.normalize("NFC", str(valor or ""))
    texto = "".join(ch for ch in texto if ch == "\n" or ch >= " ")
    texto = re.sub(r"[ \t]+", " ", texto).strip()
    return texto[:limite]


@dataclass(frozen=True)
class TextoEntrada:
    """O que vai para o modelo, com a proveniência do que foi enviado."""

    texto: str
    url_original: str
    trecho: str
    evento_id: int | None = None

    @property
    def hash_entrada(self) -> str:
        """SHA-256 do texto **exato** enviado (hex de 64, como a proveniência)."""
        return hashlib.sha256(self.texto.encode("utf-8")).hexdigest()

    def resumo(self) -> dict:
        """Retrato para log/relatório: sem conteúdo integral."""
        return {
            "url_original": self.url_original,
            "hash_entrada": self.hash_entrada,
            "caracteres": len(self.texto),
            "evento_id": self.evento_id,
        }


def texto_de_evento(
    *,
    titulo: str,
    local: str = "",
    descricao: str = "",
    trecho: str = "",
    url_original: str = "",
    categoria: str = "",
    preco: str = "",
    evento_id: int | None = None,
) -> TextoEntrada:
    """Monta a entrada a partir de campos já extraídos — nunca da página crua.

    Envia-se o mínimo necessário (título, local, preço, categoria declarada e o
    trecho que o coletor já guardou como evidência). Descrição entra truncada.
    A URL da fonte viaja como proveniência, não como conteúdo a interpretar.
    """
    partes = [f"titulo: {_limpar(titulo, 300)}"]
    for rotulo, valor in (
        ("local", local),
        ("preco", preco),
        ("categoria_declarada", categoria),
        ("descricao", descricao),
        ("trecho_fonte", trecho),
    ):
        limpo = _limpar(valor, LIMITE_TEXTO)
        if limpo:
            partes.append(f"{rotulo}: {limpo}")
    texto = "\n".join(partes)[:LIMITE_TEXTO]
    return TextoEntrada(
        texto=texto,
        url_original=_limpar(url_original, 500),
        trecho=_limpar(trecho, LIMITE_TRECHO),
        evento_id=evento_id,
    )


@dataclass(frozen=True)
class SugestaoIA:
    """Classificação proposta pelo modelo. Consultiva, nunca decisória."""

    categoria: str = ""
    publico: str = ""
    faixa_etaria: str = ""
    gratuito: bool | None = None
    acessibilidade: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    resumo: str = ""
    confianca: float = 0.0
    justificativa: str = ""
    campos_incertos: tuple[str, ...] = ()
    # Procedência da sugestão (quem respondeu, com que prompt, sobre qual texto).
    modelo: str = ""
    provedor: str = "refuseless"
    prompt_versao: str = ""
    hash_entrada: str = ""
    url_original: str = ""
    trecho_entrada: str = ""
    saida_json: str = ""

    @property
    def pode_aprovar(self) -> bool:
        """Não existe sugestão de IA que aprove. Constante, para teste explícito."""
        return False

    @property
    def revisao_obrigatoria(self) -> bool:
        return self.confianca < 0.6 or bool(self.campos_incertos)

    def motivos(self) -> tuple[str, ...]:
        """Texto que o moderador vê. Nunca altera status nem aprova."""
        itens: list[str] = []
        if self.categoria:
            itens.append(f"ia_sugestao:categoria={self.categoria}")
        if self.gratuito is not None:
            itens.append(f"ia_sugestao:gratuito={'sim' if self.gratuito else 'nao'}")
        itens.append(f"ia_sugestao:confianca={self.confianca:.2f}")
        if self.revisao_obrigatoria:
            itens.append("ia_sugestao:revisar")
        if self.campos_incertos:
            itens.append("ia_sugestao:incerto:" + ",".join(sorted(self.campos_incertos)))
        return tuple(itens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "categoria": self.categoria,
            "publico": self.publico,
            "faixa_etaria": self.faixa_etaria,
            "gratuito": self.gratuito,
            "acessibilidade": list(self.acessibilidade),
            "tags": list(self.tags),
            "resumo": self.resumo,
            "confianca": self.confianca,
            "justificativa": self.justificativa,
            "campos_incertos": list(self.campos_incertos),
            "modelo": self.modelo,
            "provedor": self.provedor,
            "prompt_versao": self.prompt_versao,
            "hash_entrada": self.hash_entrada,
            "url_original": self.url_original,
            "revisao_obrigatoria": self.revisao_obrigatoria,
            "pode_aprovar": self.pode_aprovar,
        }


def tags_unicas(valores: Iterable[str], limite: int = 8) -> tuple[str, ...]:
    vistas: list[str] = []
    for valor in valores:
        # Translitera antes de filtrar: "música" vira "musica", não "msica".
        base = unicodedata.normalize("NFKD", str(valor or "").strip().lower())
        base = "".join(c for c in base if not unicodedata.combining(c))
        tag = re.sub(r"[^a-z0-9_\-]", "", base)[:32]
        if tag and tag not in vistas:
            vistas.append(tag)
        if len(vistas) >= limite:
            break
    return tuple(vistas)


__all__ = [
    "ACESSIBILIDADES",
    "CATEGORIAS",
    "FAIXAS",
    "LIMITE_TEXTO",
    "LIMITE_TRECHO",
    "PUBLICOS",
    "SugestaoIA",
    "TextoEntrada",
    "tags_unicas",
    "texto_de_evento",
]
