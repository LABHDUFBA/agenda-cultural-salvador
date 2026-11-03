"""Interface comum dos coletores.

Todo coletor concreto segue o mesmo contrato:

* recebe uma :class:`sources.catalog.FonteCultural` (URL canônica) e um
  :class:`collectors.fetch.HttpFetcher` (política de rede);
* implementa ``_coletar()`` devolvendo ``list[EventoColetado]``;
* ``coletar()`` ordena, deduplica e aplica o limite;
* ``verificar_acesso()`` faz **uma** requisição e descreve o resultado.

Nenhum coletor aprova, publica ou grava. Nenhum coletor tenta contornar
bloqueio: erros de rede/robots/WAF sobem como exceções classificadas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sources.catalog import FonteCultural

from .fetch import ErroColeta, HttpFetcher, PaginaBaixada
from .models import EventoColetado


class ColetorBase:
    """Base abstrata; subclasses implementam ``_coletar``."""

    def __init__(self, fonte: FonteCultural, fetcher: HttpFetcher | None = None) -> None:
        self.fonte = fonte
        self.fetcher = fetcher or HttpFetcher()

    # -- contrato ----------------------------------------------------------
    def coletar(
        self,
        *,
        limite: int | None = None,
        desde: datetime | None = None,
        ate: datetime | None = None,
    ) -> list[EventoColetado]:
        eventos = self._coletar()
        eventos = self._filtrar_periodo(eventos, desde=desde, ate=ate)
        eventos = self._deduplicar(eventos)
        eventos.sort(key=lambda e: (e.inicio_em, e.titulo))
        if limite is not None:
            if limite < 0:
                raise ValueError("limite não pode ser negativo")
            eventos = eventos[:limite]
        return eventos

    def _coletar(self) -> list[EventoColetado]:  # pragma: no cover - abstrato
        raise NotImplementedError

    def verificar_acesso(self) -> dict[str, object]:
        resultado = self.fetcher.sondar(self.fonte.url, fonte_id=self.fonte.id)
        resultado["fonte_id"] = self.fonte.id
        resultado["nome"] = self.fonte.nome
        resultado["tipo_conteudo"] = self.fonte.tipo_conteudo
        return resultado

    # -- utilidades --------------------------------------------------------
    def baixar(self) -> PaginaBaixada:
        return self.fetcher.baixar(self.fonte.url, fonte_id=self.fonte.id)

    def _filtrar_periodo(
        self,
        eventos: list[EventoColetado],
        *,
        desde: datetime | None,
        ate: datetime | None,
    ) -> list[EventoColetado]:
        if desde is None and ate is None:
            return eventos
        filtrados = []
        for evento in eventos:
            if desde is not None and evento.inicio_em < desde:
                continue
            if ate is not None and evento.inicio_em > ate:
                continue
            filtrados.append(evento)
        return filtrados

    @staticmethod
    def _deduplicar(eventos: list[EventoColetado]) -> list[EventoColetado]:
        vistos: set[tuple[str, str, str]] = set()
        unicos: list[EventoColetado] = []
        for evento in eventos:
            chave = (evento.titulo.casefold(), evento.inicio_em.isoformat(), evento.local_nome.casefold())
            if chave in vistos:
                continue
            vistos.add(chave)
            unicos.append(evento)
        return unicos


def janela_padrao(dias_passado: int = 1, dias_futuro: int = 400) -> tuple[datetime, datetime]:
    """Janela de saneamento usada pelo CLI e pelo agendador."""
    agora = datetime.now(timezone.utc)
    return agora - timedelta(days=dias_passado), agora + timedelta(days=dias_futuro)


__all__ = ["ColetorBase", "janela_padrao", "ErroColeta"]
