"""Validação conservadora de eventos para Salvador (BA).

Princípio: nada é aprovado automaticamente. A validação só decide entre
`pendente` (entra na fila de revisão) e `rejeitado` (fica registrado para
auditoria, com motivo). A aprovação é sempre humana.

Regras, em ordem:

- título vazio ou com menos de 3 caracteres ........................ rejeita
- município explícito diferente de Salvador ........................ rejeita
- UF explícita diferente de BA ..................................... rejeita
- coordenadas fora da caixa de Salvador ............................ rejeita
- município ausente e sem coordenadas dentro da caixa .............. rejeita
- município ausente mas coordenadas dentro da caixa ................ pendente
  (motivo `municipio_inferido_por_coordenadas`)
- UF ausente ....................................................... pendente
  (motivo `uf_ausente`)
- início não interpretável ......................................... rejeita
- fim anterior ao início ........................................... rejeita
- início mais de 2 anos no futuro .................................. rejeita
- evento já encerrado há mais de 1 dia ............................. rejeita
- URL de ingresso que não seja http(s) ............................. rejeita

Códigos de motivo podem vir como "codigo" ou "codigo:valor".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from .dedup import data_ref_local, normalizar_texto
from .models import Evento, agora_utc, para_dt

# Caixa generosa em volta do município de Salvador.
LAT_MIN, LAT_MAX = -13.10, -12.75
LON_MIN, LON_MAX = -38.62, -38.30

HORIZONTE_FUTURO = timedelta(days=730)
TOLERANCIA_ENCERRADO = timedelta(days=1)
TITULO_MINIMO = 3

STATUS_PENDENTE = "pendente"
STATUS_REJEITADO = "rejeitado"
STATUS_APROVADO = "aprovado"


@dataclass(frozen=True, slots=True)
class Decisao:
    status: str
    motivos: tuple[str, ...] = ()
    evidencias: tuple[str, ...] = ()

    @property
    def rejeitado(self) -> bool:
        return self.status == STATUS_REJEITADO


def coordenadas_em_salvador(latitude: float, longitude: float) -> bool:
    return LAT_MIN <= latitude <= LAT_MAX and LON_MIN <= longitude <= LON_MAX


def _validar_datas(evento: Evento, agora: datetime) -> list[str]:
    erros: list[str] = []
    try:
        inicio = para_dt(evento.inicio_em)
    except (ValueError, TypeError):
        return ["inicio_invalido"]

    if inicio > agora + HORIZONTE_FUTURO:
        erros.append("inicio_implausivel")

    fim = None
    if evento.fim_em is not None:
        try:
            fim = para_dt(evento.fim_em)
        except (ValueError, TypeError):
            erros.append("fim_invalido")
        else:
            if fim < inicio:
                erros.append("fim_antes_do_inicio")

    referencia = fim if fim is not None else inicio
    if referencia < agora - TOLERANCIA_ENCERRADO:
        erros.append("evento_encerrado")

    return erros


def validar_evento(evento: Evento, *, agora: datetime | None = None) -> Decisao:
    agora = agora or agora_utc()
    erros: list[str] = []
    avisos: list[str] = []
    evidencias: list[str] = []

    titulo = normalizar_texto(evento.titulo)
    if len(titulo) < TITULO_MINIMO:
        erros.append("titulo_invalido")

    municipio = normalizar_texto(evento.municipio)
    uf = (evento.uf or "").strip().upper()

    latitude, longitude = None, None
    if evento.latitude is not None and evento.longitude is not None:
        try:
            latitude, longitude = float(evento.latitude), float(evento.longitude)
        except (TypeError, ValueError):
            erros.append("coordenadas_invalidas")

    tem_coordenadas = latitude is not None and longitude is not None
    if (evento.latitude is None) != (evento.longitude is None):
        erros.append("coordenadas_incompletas")
    elif tem_coordenadas and not coordenadas_em_salvador(latitude, longitude):
        erros.append("coordenadas_fora_de_salvador")

    if municipio:
        if municipio != "salvador":
            erros.append(f"fora_de_salvador:{municipio}")
        else:
            evidencias.append("municipio_salvador")
    else:
        if tem_coordenadas and coordenadas_em_salvador(latitude, longitude):
            avisos.append("municipio_inferido_por_coordenadas")
        else:
            erros.append("salvador_nao_comprovado")

    if uf:
        if uf != "BA":
            erros.append(f"uf_incompativel:{uf}")
        else:
            evidencias.append("uf_ba")
    else:
        avisos.append("uf_ausente")

    if tem_coordenadas and coordenadas_em_salvador(latitude, longitude):
        evidencias.append("coordenadas_salvador")

    if evento.url_ingresso:
        url = evento.url_ingresso.strip().lower()
        if not (url.startswith("http://") or url.startswith("https://")):
            erros.append("url_ingresso_invalida")

    erros_datas = _validar_datas(evento, agora)
    erros.extend(erros_datas)

    if erros:
        return Decisao(STATUS_REJEITADO, tuple(erros), tuple(evidencias))
    return Decisao(STATUS_PENDENTE, tuple(avisos), tuple(evidencias))


# ---------------------------------------------------------------------------
# Portão de publicação (usado pelo bot, nunca pelo coletor)
#
# Publicar exige mais do que um status. O evento precisa provar, no banco
# canônico, que é em Salvador, que tem data/hora local coerente, que tem local
# e que tem fonte verificável. `proveniencias` deve trazer a confiabilidade da
# fonte (`fonte_confiabilidade`), resultado do JOIN com `fontes`.
# ---------------------------------------------------------------------------

_HEX64 = set("0123456789abcdef")
CONFIABILIDADE_PUBLICAVEL = ("alta", "media")


def _hash_valido(valor: object) -> bool:
    texto = str(valor or "").strip().lower()
    return len(texto) == 64 and set(texto) <= _HEX64


def _fonte_verificavel(evento: Mapping, proveniencias: Sequence[Mapping]) -> str | None:
    """Devolve o motivo de bloqueio ou None quando há fonte aceitável."""
    url_declarada = str(evento.get("url_fonte") or "").strip().lower()
    if url_declarada.startswith(("http://", "https://")):
        return None

    validas = [
        p
        for p in proveniencias
        if str(p.get("url_original") or "").strip().lower().startswith(("http://", "https://"))
        and _hash_valido(p.get("hash_conteudo"))
    ]
    for prov in validas:
        if str(prov.get("fonte_confiabilidade") or "").strip().lower() in CONFIABILIDADE_PUBLICAVEL:
            return None
    if validas:
        return "fonte_apenas_de_baixa_confiabilidade"
    return "fonte_nao_verificavel"


def _coordenadas_do_evento(evento: Mapping) -> tuple[float, float] | None:
    if evento.get("latitude") is None or evento.get("longitude") is None:
        return None
    try:
        return float(evento["latitude"]), float(evento["longitude"])
    except (TypeError, ValueError):
        return None


def evidencia_de_municipio(
    evento: Mapping, proveniencias: Sequence[Mapping] = ()
) -> str:
    """Base factual que prova (ou não) que o evento é em Salvador.

    O campo ``municipio`` é uma **declaração**, não uma prova: tanto o coletor
    quanto o formulário do bot preenchem ``Salvador`` por padrão. Publicar
    exige uma destas evidências independentes:

    * ``coordenadas_salvador`` — latitude/longitude gravadas dentro da caixa do
      município;
    * ``trecho_fonte_salvador`` — o trecho capturado da fonte nomeia Salvador;
    * ``declaracao_operador`` — submit humano identificado + autorização
      confirmada com referência (alguém responde pelo fato, com registro).

    Devolve a base encontrada ou string vazia quando só existe a declaração.
    """
    coords = _coordenadas_do_evento(evento)
    if coords is not None and coordenadas_em_salvador(*coords):
        return "coordenadas_salvador"
    for prov in proveniencias:
        trecho = normalizar_texto(str(prov.get("trecho") or ""))
        # Casa "Salvador", "Salvador-BA", "em Salvador/BA" — mas não
        # "Salvadorzinho" nem nomes próprios que apenas contêm a palavra.
        if re.search(r"(?<![a-z0-9])salvador(?![a-z0-9])", trecho):
            return "trecho_fonte_salvador"
    if (
        str(evento.get("enviado_por") or "0") != "0"
        and bool(evento.get("autorizacao_confirmada"))
        and str(evento.get("autorizacao_referencia") or "").strip()
    ):
        return "declaracao_operador"
    return ""


def requisitos_publicacao(
    evento: Mapping,
    proveniencias: Sequence[Mapping] = (),
    *,
    agora: datetime | None = None,
) -> tuple[str, ...]:
    """Lista o que impede publicar. Tupla vazia == publicável.

    Somente leitura e determinístico: a decisão final continua sendo do
    administrador, mas nenhum evento sem estas evidências passa.
    """
    agora = agora or agora_utc()
    faltas: list[str] = []

    if str(evento.get("status") or "") != STATUS_APROVADO:
        faltas.append("evento_sem_aprovacao_editorial")

    if normalizar_texto(str(evento.get("municipio") or "")) != "salvador":
        faltas.append("municipio_salvador_nao_comprovado")
    else:
        coords = _coordenadas_do_evento(evento)
        if coords is not None and not coordenadas_em_salvador(*coords):
            # Declarar Salvador e apontar para fora da cidade é contradição,
            # não evidência: nenhuma das duas fontes pode ser descartada.
            faltas.append("municipio_salvador_contradito_por_coordenadas")
        elif not evidencia_de_municipio(evento, proveniencias):
            # "Salvador" digitado por padrão não prova nada.
            faltas.append("municipio_salvador_sem_evidencia")

    if str(evento.get("uf") or "").strip().upper() != "BA":
        faltas.append("uf_ba_nao_comprovada")

    if len(normalizar_texto(str(evento.get("local_nome") or ""))) < TITULO_MINIMO:
        faltas.append("local_nao_comprovado")

    inicio_iso = str(evento.get("inicio_em") or "")
    try:
        inicio = para_dt(inicio_iso)
    except (ValueError, TypeError):
        faltas.append("data_local_invalida")
    else:
        esperado = data_ref_local(inicio_iso)
        if str(evento.get("data_ref") or "") != esperado:
            faltas.append("data_ref_incoerente_com_inicio")
        if inicio > agora + HORIZONTE_FUTURO:
            faltas.append("data_local_implausivel")
        if inicio < agora - TOLERANCIA_ENCERRADO:
            faltas.append("evento_encerrado")

    fonte = _fonte_verificavel(evento, proveniencias)
    if fonte:
        faltas.append(fonte)

    if "possivel_duplicado" in str(evento.get("motivo") or ""):
        faltas.append("possivel_duplicado_nao_resolvido")

    return tuple(faltas)
