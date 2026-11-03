"""Coletores para fontes que *não* podem ser coletadas — e dizem por quê.

Existem para que o catálogo cubra 100% das fontes obrigatórias sem fingir
cobertura: a fonte aparece, o motivo fica registrado e nada é inventado.
"""

from __future__ import annotations

from .base import ColetorBase
from .fetch import FonteBloqueada, FonteNaoSuportada


class ColetorBloqueada(ColetorBase):
    """Fonte que recusou acesso ou está fora de alcance. Não tenta de novo."""

    def _coletar(self):
        raise FonteBloqueada(
            f"{self.fonte.nome}: acesso não permitido/indisponível — "
            f"{self.fonte.observacao or 'ver catálogo'}",
            status=self.fonte.http_status or 0,
            fonte_id=self.fonte.id,
            url=self.fonte.url,
        )


class ColetorNaoSuportada(ColetorBase):
    """Fonte cujo conteúdo exige JS ou API privada. Sem engenharia reversa."""

    def _coletar(self):
        raise FonteNaoSuportada(
            f"{self.fonte.nome}: {self.fonte.observacao or 'formato não suportado'}",
            fonte_id=self.fonte.id,
            url=self.fonte.url,
        )


__all__ = ["ColetorBloqueada", "ColetorNaoSuportada"]
