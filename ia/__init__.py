"""Integração **opcional** de IA (Refuseless) para extração/classificação.

Desligada por padrão. Quando ligada, produz sugestões auditáveis sobre eventos
já coletados — nunca aprova, nunca publica, nunca altera o status de nada.

    from ia import IaConfig, EnriquecimentoIA
    config = IaConfig.from_env()
    if config.habilitada:
        ia = EnriquecimentoIA(conn, config)
        ia.processar_lote([(evento_coletado, evento_id)])

Ver docs/ia.md.
"""

from .cliente import (
    ClienteRefuseless,
    ErroConfiguracaoIA,
    ErroIA,
    ErroRespostaIA,
    ErroTransporteIA,
    RespostaIA,
    sanitizar,
)
from .config import BASE_PADRAO, MODELO_PADRAO, VARIAVEL_CHAVE, IaConfig
from .enriquecimento import (
    EnriquecimentoIA,
    ResultadoEnriquecimento,
    entrada_do_evento,
    motivo_elegibilidade,
)
from .esquema import PROMPT_VERSAO, SaidaInvalida, validar_saida
from .modelos import SugestaoIA, TextoEntrada, texto_de_evento
from .repository import (
    aceitar_sugestao,
    chamadas_no_dia,
    descartar_sugestao,
    listar_sugestoes,
    resumo_consumo,
    sugestoes_do_evento,
)

__all__ = [
    "BASE_PADRAO",
    "MODELO_PADRAO",
    "PROMPT_VERSAO",
    "VARIAVEL_CHAVE",
    "ClienteRefuseless",
    "EnriquecimentoIA",
    "ErroConfiguracaoIA",
    "ErroIA",
    "ErroRespostaIA",
    "ErroTransporteIA",
    "IaConfig",
    "RespostaIA",
    "ResultadoEnriquecimento",
    "SaidaInvalida",
    "SugestaoIA",
    "TextoEntrada",
    "aceitar_sugestao",
    "chamadas_no_dia",
    "descartar_sugestao",
    "entrada_do_evento",
    "listar_sugestoes",
    "motivo_elegibilidade",
    "resumo_consumo",
    "sanitizar",
    "sugestoes_do_evento",
    "texto_de_evento",
    "validar_saida",
]
