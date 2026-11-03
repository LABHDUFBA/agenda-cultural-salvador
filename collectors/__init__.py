"""Coletores da agenda cultural de Salvador.

Uso::

    from sources.catalog import por_id
    from collectors import obter_coletor

    coletor = obter_coletor(por_id("tca"))
    eventos = coletor.coletar(limite=10)
    evento = eventos[0]
    evento.evento_kwargs()        # -> database.models.Evento
    evento.proveniencia_kwargs(fonte_id=1, sessao_id=2, capturado_em=...)

Somente biblioteca padrão (regra do projeto: `requirements.txt` vazio).
"""

from .base import ColetorBase, janela_padrao
from .fetch import (
    ErroColeta,
    FonteBloqueada,
    FonteIndisponivel,
    FonteNaoSuportada,
    HttpFetcher,
    RespostaInvalida,
    RobotsProibido,
)
from .models import EventoColetado
from .registry import REGISTRO, obter_coletor, obter_por_id, todos_os_coletores

__all__ = [
    "ColetorBase",
    "ErroColeta",
    "EventoColetado",
    "FonteBloqueada",
    "FonteIndisponivel",
    "FonteNaoSuportada",
    "HttpFetcher",
    "REGISTRO",
    "RespostaInvalida",
    "RobotsProibido",
    "janela_padrao",
    "obter_coletor",
    "obter_por_id",
    "todos_os_coletores",
]
