"""Registro de coletores: mapeia ``FonteCultural.coletor`` para a classe."""

from __future__ import annotations

from typing import Callable

from sources.catalog import CATALOGO, FonteCultural, por_id

from .bagov import ColetorBaGovCalendario
from .base import ColetorBase
from .bilheteria_digital import ColetorBilheteriaDigital
from .casa_rosa import ColetorCasaRosa
from .cine_glauber import ColetorCineGlauber
from .fetch import HttpFetcher
from .nulls import ColetorBloqueada, ColetorNaoSuportada
from .salvador_da_bahia import ColetorSalvadorDaBahia
from .sympla import ColetorSympla
from .ufba_agenda import ColetorAgendaUFBA
from .ufba_teatro import ColetorTeatroUFBA
from .vila_velha import ColetorVilaVelha

Fabrica = Callable[[FonteCultural, HttpFetcher], ColetorBase]

REGISTRO: dict[str, type[ColetorBase]] = {
    "bagov_calendario": ColetorBaGovCalendario,
    "salvador_da_bahia_cartoes": ColetorSalvadorDaBahia,
    "vila_velha_texto": ColetorVilaVelha,
    "sympla_payload": ColetorSympla,
    "casa_rosa_widget": ColetorCasaRosa,
    "bilheteria_digital_cartoes": ColetorBilheteriaDigital,
    "ufba_agenda_tabela": ColetorAgendaUFBA,
    "ufba_teatro_json": ColetorTeatroUFBA,
    "cine_glauber_sessoes": ColetorCineGlauber,
    "bloqueada": ColetorBloqueada,
    "nao_suportada": ColetorNaoSuportada,
}


def obter_coletor(fonte: FonteCultural, fetcher: HttpFetcher | None = None) -> ColetorBase:
    try:
        classe = REGISTRO[fonte.coletor]
    except KeyError:
        raise KeyError(
            f"coletor {fonte.coletor!r} não registrado para a fonte {fonte.id!r}"
        ) from None
    return classe(fonte, fetcher or HttpFetcher())


def obter_por_id(fonte_id: str, fetcher: HttpFetcher | None = None) -> ColetorBase:
    return obter_coletor(por_id(fonte_id), fetcher)


def todos_os_coletores(fetcher: HttpFetcher | None = None) -> dict[str, ColetorBase]:
    cliente = fetcher or HttpFetcher()
    return {fonte.id: obter_coletor(fonte, cliente) for fonte in CATALOGO}


__all__ = ["REGISTRO", "obter_coletor", "obter_por_id", "todos_os_coletores"]
