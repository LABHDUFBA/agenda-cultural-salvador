"""Feiras livres e culturais de Salvador: catálogo com evidência + consulta.

    from feiras import carregar, listar
    carregar(conn)
    for feira in listar(conn, dia="sabado"):
        print(feira.nome, feira.bairro)
"""

from .catalogo import (
    CAPTURADO_EM,
    DIAS,
    FEIRAS,
    FONTES,
    Evidencia,
    Feira,
    FonteFeira,
    normalizar_dia,
    por_slug,
)
from .repository import (
    EvidenciaRegistro,
    FeiraRegistro,
    ResumoCarregamento,
    buscar,
    carregar,
    evidencias,
    fontes,
    listar,
    obter,
    resumo,
)

__all__ = [
    "CAPTURADO_EM",
    "DIAS",
    "FEIRAS",
    "FONTES",
    "Evidencia",
    "Feira",
    "FonteFeira",
    "normalizar_dia",
    "por_slug",
    "EvidenciaRegistro",
    "FeiraRegistro",
    "ResumoCarregamento",
    "buscar",
    "carregar",
    "evidencias",
    "fontes",
    "listar",
    "obter",
    "resumo",
]
