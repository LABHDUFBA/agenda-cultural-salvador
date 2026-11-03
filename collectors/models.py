"""Contrato de saída dos coletores.

O coletor **não** grava no banco e **não** decide status editorial. Ele devolve
:class:`EventoColetado`, que carrega o conteúdo, a evidência (trecho) e a
metainformação de extração (método, confiança, o que foi inferido).

A ponte com o núcleo de dados é feita por dois métodos que devolvem ``kwargs``
compatíveis com ``database.models.Evento`` e ``database.models.Proveniencia``
— sem importar o pacote ``database`` (evita acoplamento e ciclo de import).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

CONFIANCAS = ("alta", "media", "baixa")
METODOS = (
    "json_embutido",     # JSON estruturado publicado pela própria página
    "cartao_html",       # cartão de listagem com data no cabeçalho
    "texto_agenda",      # parsing de texto corrido (frágil)
    "api_publica",       # endpoint público documentado
    "atributo_html",     # valor em campo/atributo renderizado pelo servidor
)

_ESPACOS = re.compile(r"\s+")


def limpar_texto(valor: str | None, limite: int = 2000) -> str:
    """Colapsa espaços, remove controle invisível e trunca com segurança."""
    if not valor:
        return ""
    texto = unicodedata.normalize("NFC", str(valor))
    texto = "".join(ch for ch in texto if ch == "\n" or ch >= " " or ch == "\t")
    texto = _ESPACOS.sub(" ", texto).strip()
    if len(texto) > limite:
        texto = texto[: limite - 1].rstrip() + "…"
    return texto


@dataclass(frozen=True, slots=True)
class EventoColetado:
    """Evento candidato extraído de uma fonte, com sua evidência."""

    fonte_id: str
    titulo: str
    inicio_em: datetime                 # sempre com tzinfo (America/Bahia ou UTC)
    url_original: str
    metodo_extracao: str
    confianca: str
    trecho: str
    fim_em: datetime | None = None
    descricao: str = ""
    local_nome: str = ""
    endereco: str = ""
    bairro: str = ""
    categoria: str = ""
    preco: str = ""
    url_ingresso: str = ""
    municipio: str = "Salvador"
    uf: str = "BA"
    latitude: float | None = None
    longitude: float | None = None
    # Campos que o coletor preencheu por inferência (hora, ano, local…).
    campos_inferidos: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        titulo = limpar_texto(self.titulo, 300)
        if len(titulo) < 3:
            raise ValueError(f"título curto demais: {self.titulo!r}")
        object.__setattr__(self, "titulo", titulo)
        if not self.url_original.lower().startswith(("http://", "https://")):
            raise ValueError(f"url_original inválida: {self.url_original!r}")
        if self.inicio_em.tzinfo is None:
            raise ValueError("inicio_em precisa de tzinfo")
        if self.confianca not in CONFIANCAS:
            raise ValueError(f"confiança inválida: {self.confianca!r}")
        if self.metodo_extracao not in METODOS:
            raise ValueError(f"método inválido: {self.metodo_extracao!r}")
        if self.fim_em is not None and self.fim_em < self.inicio_em:
            raise ValueError("fim_em anterior a inicio_em")
        object.__setattr__(self, "trecho", limpar_texto(self.trecho, 500))
        object.__setattr__(self, "descricao", limpar_texto(self.descricao))

    @property
    def revisao_necessaria(self) -> bool:
        """Baixa confiança ou qualquer inferência obrigam revisão humana."""
        return bool(self.campos_inferidos) or self.confianca == "baixa"

    def hash_conteudo(self) -> str:
        """SHA-256 estável do conteúdo normalizado (exigido pela proveniência)."""
        base = "|".join(
            [
                self.titulo.casefold(),
                self.inicio_em.isoformat(),
                limpar_texto(self.local_nome, 200).casefold(),
                self.url_original.split("?", 1)[0],
                self.trecho,
            ]
        )
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Forma serializável (JSON) para relatórios e CLI."""
        return {
            "fonte_id": self.fonte_id,
            "titulo": self.titulo,
            "inicio_em": self.inicio_em.isoformat(),
            "fim_em": self.fim_em.isoformat() if self.fim_em else None,
            "url_original": self.url_original,
            "descricao": self.descricao,
            "local_nome": self.local_nome,
            "endereco": self.endereco,
            "bairro": self.bairro,
            "categoria": self.categoria,
            "preco": self.preco,
            "url_ingresso": self.url_ingresso,
            "municipio": self.municipio,
            "uf": self.uf,
            "metodo_extracao": self.metodo_extracao,
            "confianca": self.confianca,
            "campos_inferidos": list(self.campos_inferidos),
            "revisao_necessaria": self.revisao_necessaria,
            "trecho": self.trecho,
            "hash_conteudo": self.hash_conteudo(),
        }

    def evento_kwargs(self) -> dict[str, Any]:
        """``kwargs`` para ``database.models.Evento``."""
        return {
            "titulo": self.titulo,
            "inicio_em": self.inicio_em,
            "municipio": self.municipio,
            "uf": self.uf,
            "local_nome": self.local_nome,
            "descricao": self.descricao,
            "fim_em": self.fim_em,
            "endereco": self.endereco,
            "bairro": self.bairro,
            "categoria": self.categoria,
            "url_ingresso": self.url_ingresso,
            "preco": self.preco,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }

    def proveniencia_kwargs(
        self, *, fonte_id: int, sessao_id: int | None, capturado_em: datetime
    ) -> dict[str, Any]:
        """``kwargs`` para ``database.models.Proveniencia``."""
        return {
            "fonte_id": fonte_id,
            "url_original": self.url_original,
            "sessao_id": sessao_id,
            "capturado_em": capturado_em,
            "hash_conteudo": self.hash_conteudo(),
            "trecho": self.trecho,
        }


__all__ = ["CONFIANCAS", "METODOS", "EventoColetado", "limpar_texto"]
