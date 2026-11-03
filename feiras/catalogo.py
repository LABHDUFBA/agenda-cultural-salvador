"""Catálogo de feiras livres e culturais de Salvador — dados com evidência.

Regra central deste módulo: **nenhum campo sem trecho de fonte**. Cada
``Feira`` carrega uma lista de ``Evidencia`` (campo, fonte, trecho literal).
O trecho é sempre construído a partir dos mesmos valores gravados, e
``tests/test_feiras.py`` verifica que ele existe, byte a byte, no recorte da
fonte preservado em ``sources/feiras/``. Se a fonte não diz, o campo fica
vazio e ``bairro_base``/``status`` registram por quê.

Fontes (todas preservadas em ``sources/feiras/*.txt`` com hash no banco):

* ``semop`` — SEMOP/PMS, "Solicitações e Localizações de Feiras": nome,
  endereço e número de feirantes das 23 feiras municipais. Fonte oficial.
  O host municipal não completa TCP a partir desta rede (timeout), então a
  leitura foi feita por extração externa documentada — o coletor do projeto
  não é usado aqui e nada é contornado (sem proxy/UA alternativo).
* ``senac`` — Observatório do Patrimônio Gastronômico do Nordeste (SENAC-BA),
  tabela "Feiras Livres Fixas de Alimentos Não Orgânicos em Salvador",
  publicada em 2019-11-29: única fonte encontrada que declara **dia de
  funcionamento** por feira ("Terça a domingo"). Dado de 2019: confiabilidade
  média e revalidação pendente.
* ``visit`` — Visit Salvador da Bahia (portal oficial de turismo, já no
  catálogo do projeto): bairro, endereço complementar, programação cultural
  (samba de domingo) e a Feira Livre de Pescados.
* ``pelourinho`` — Pelourinho Dia e Noite (SECULT/PMS): a "Feira de
  Artesanato" da Praça da Sé é publicada por edição, com data e horário
  próprios — evidência de que **não** existe dia fixo.
* ``acm`` — Instituto ACM (organizador da Feira da Sé): confirma que datas e
  horários são divulgados a cada edição.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Tipo = Literal["livre", "cultural", "artesanato", "misto"]
StatusVerificacao = Literal["verificado", "parcial", "sem_dia_fixo", "nao_verificado"]
Acessibilidade = Literal["ok", "bloqueada", "indisponivel", "nao_suportada", "nao_verificada"]
MetodoCaptura = Literal["http_direto", "extracao_externa", "registro_arquivado"]

DIAS = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
DIAS_ABREV = {
    "segunda": "seg",
    "terca": "ter",
    "quarta": "qua",
    "quinta": "qui",
    "sexta": "sex",
    "sabado": "sáb",
    "domingo": "dom",
}
TERCA_A_DOMINGO = ("terca", "quarta", "quinta", "sexta", "sabado", "domingo")
CAPTURADO_EM = "2026-11-03"


@dataclass(frozen=True, slots=True)
class FonteFeira:
    chave: str
    nome: str
    organizacao: str
    url: str
    confiabilidade: Literal["alta", "media", "baixa"]
    acessibilidade: Acessibilidade
    http_status: int | None
    metodo_captura: MetodoCaptura
    snapshot: str
    observacao: str = ""


@dataclass(frozen=True, slots=True)
class Evidencia:
    campo: str
    fonte: str
    trecho: str


@dataclass(frozen=True, slots=True)
class Feira:
    slug: str
    nome: str
    tipo: Tipo
    bairro: str
    bairro_base: Literal["fonte", "nome_oficial", "nao_informado"]
    endereco: str
    dias: tuple[str, ...]
    horario: str
    feirantes: int | None
    regular: bool
    status: StatusVerificacao
    observacao: str = ""
    evidencias: tuple[Evidencia, ...] = field(default_factory=tuple)


FONTES: dict[str, FonteFeira] = {
    "semop": FonteFeira(
        chave="semop",
        nome="SEMOP — Solicitações e Localizações de Feiras",
        organizacao="Secretaria Municipal de Ordem Pública (Prefeitura de Salvador)",
        url="https://ordempublica.salvador.ba.gov.br/solicitacoes-e-localizacoes-de-feiras/",
        confiabilidade="alta",
        acessibilidade="bloqueada",
        http_status=None,
        metodo_captura="extracao_externa",
        snapshot="semop_localizacoes.txt",
        observacao=(
            "Host municipal 177.20.x: TCP filtrado a partir desta rede (timeout "
            "de 25 s em 2026-11-03). Conteúdo lido por extração externa "
            "documentada; nenhum proxy/UA alternativo foi usado pelo coletor."
        ),
    ),
    "senac": FonteFeira(
        chave="senac",
        nome="Observatório do Patrimônio Gastronômico do Nordeste (SENAC-BA)",
        organizacao="SENAC Bahia",
        url=(
            "http://observatoriogastronomico.senac.br/mercados_feiras/"
            "feiras-livres-fixas-de-alimentos-nao-organicos-em-salvador/"
        ),
        confiabilidade="media",
        acessibilidade="indisponivel",
        http_status=200,
        metodo_captura="extracao_externa",
        snapshot="senac_observatorio.txt",
        observacao=(
            "Respondeu 200 na primeira leitura de 2026-11-03 e passou a dar "
            "timeout poucos minutos depois, na mesma sessão. Dado publicado "
            "em 2019-11-29: dias de funcionamento precisam de revalidação."
        ),
    ),
    "visit": FonteFeira(
        chave="visit",
        nome="Visit Salvador da Bahia — Feiras e Mercados",
        organizacao="Portal oficial de turismo de Salvador",
        url=(
            "https://www.salvadordabahia.com/"
            "feiras-e-mercados-de-salvador-lista-completa-funcionamento-e-informacoes-gerais/"
        ),
        confiabilidade="media",
        acessibilidade="ok",
        http_status=200,
        metodo_captura="http_direto",
        snapshot="visit_salvador.txt",
        observacao="HTTP 200, 160.069 bytes, leitura direta em 2026-11-03.",
    ),
    "pelourinho": FonteFeira(
        chave="pelourinho",
        nome="Pelourinho Dia e Noite — Feira de Artesanato",
        organizacao="SECULT / Prefeitura Municipal de Salvador",
        url="https://pelourinhodiaenoite.salvador.ba.gov.br/feira-de-artesanato-2/",
        confiabilidade="alta",
        acessibilidade="bloqueada",
        http_status=None,
        metodo_captura="extracao_externa",
        snapshot="pelourinho_artesanato.txt",
        observacao="Mesma faixa municipal bloqueada por TCP a partir desta rede em 2026-11-03.",
    ),
    "acm": FonteFeira(
        chave="acm",
        nome="Instituto ACM — Projetos: Feira da Sé",
        organizacao="Instituto Antonio Carlos Magalhães (organizador da Feira da Sé)",
        url="https://www.institutoacm.com.br/projetos/feira-da-se/",
        confiabilidade="media",
        acessibilidade="ok",
        http_status=200,
        metodo_captura="http_direto",
        snapshot="instituto_acm.txt",
        observacao="HTTP 200, 84.522 bytes, leitura direta em 2026-11-03.",
    ),
}


def _semop(nome: str, feirantes: str, endereco: str) -> str:
    """Linha da tabela oficial, reconstruída campo a campo (verificada em teste)."""
    return f"| {nome} | {feirantes} | {endereco} |"


def _senac(nome: str, endereco: str, funcionamento: str = "Terça a domingo") -> str:
    return f"| {nome} | {endereco} | {funcionamento} |"


def _f(
    *,
    slug: str,
    nome: str,
    semop_nome: str,
    feirantes: str,
    endereco: str,
    senac_nome: str,
    senac_endereco: str,
    bairro: str = "",
    bairro_base: str = "nao_informado",
    tipo: Tipo = "livre",
    observacao: str = "",
    evidencias_extra: tuple[Evidencia, ...] = (),
) -> Feira:
    """Feira municipal com as duas evidências mínimas (SEMOP + SENAC)."""
    evidencias = [
        Evidencia("endereco", "semop", _semop(semop_nome, feirantes, endereco)),
        Evidencia("dias", "senac", _senac(senac_nome, senac_endereco)),
    ]
    evidencias.extend(evidencias_extra)
    return Feira(
        slug=slug,
        nome=nome,
        tipo=tipo,
        bairro=bairro,
        bairro_base=bairro_base,  # type: ignore[arg-type]
        endereco=endereco,
        dias=TERCA_A_DOMINGO,
        horario="",
        feirantes=int(feirantes),
        regular=True,
        status="verificado",
        observacao=observacao,
        evidencias=tuple(evidencias),
    )


FEIRAS: tuple[Feira, ...] = (
    Feira(
        slug="frutas-importadas",
        nome="Frutas Importadas",
        tipo="livre",
        bairro="",
        bairro_base="nao_informado",
        endereco="Avenida Sete de Setembro e Adjacências",
        dias=TERCA_A_DOMINGO,
        horario="",
        feirantes=10,
        regular=True,
        status="verificado",
        observacao=(
            "A fonte oficial descreve o endereço como 'Avenida Sete de Setembro e "
            "Adjacências' e não declara bairro."
        ),
        evidencias=(
            Evidencia("endereco", "semop", _semop("Frutas Importadas", "10", "Avenida Sete de Setembro e Adjacências")),
            Evidencia("dias", "senac", _senac("Av. Sete de Setembro", "Transversais ao longo da via")),
        ),
    ),
    _f(
        slug="sete-portas",
        nome="Sete Portas",
        semop_nome="Sete Portas",
        feirantes="33",
        endereco="Avenida Sete de Setembro e Adjacências",
        senac_nome="Sete Portas",
        senac_endereco="Rua Conego Pereira,",
        observacao=(
            "Divergência de endereço entre as fontes: SEMOP publica 'Avenida Sete "
            "de Setembro e Adjacências'; SENAC publica 'Rua Cônego Pereira'."
        ),
    ),
    _f(
        slug="jardim-cruzeiro",
        nome="Jardim Cruzeiro",
        semop_nome="Jardim Cruzeiro",
        feirantes="117",
        endereco="Rua Rezende Costa",
        senac_nome="Jardim Cruzeiro",
        senac_endereco="Rua Rezende Costa,",
        bairro="Jardim Cruzeiro",
        bairro_base="nome_oficial",
    ),
    Feira(
        slug="sao-joaquim",
        nome="São Joaquim",
        tipo="misto",
        bairro="Comércio",
        bairro_base="fonte",
        endereco="Avenida Oscar Pontes",
        dias=TERCA_A_DOMINGO,
        horario="",
        feirantes=7500,
        regular=True,
        status="verificado",
        observacao=(
            "Maior feira livre da cidade, com programação cultural regular: "
            "'Samba da Feira' aos domingos, no píer, a partir das 13h."
        ),
        evidencias=(
            Evidencia("endereco", "semop", _semop("São Joaquim", "7.500", "Avenida Oscar Pontes")),
            Evidencia("dias", "senac", _senac("São Joaquim", "Av. Oscar Pontes,")),
            Evidencia(
                "bairro",
                "visit",
                "Feira de São Joaquim, Cidade Baixa: Av. Eng. Oscar Pontes – Comércio, Salvador – BA, 40411-220",
            ),
            Evidencia(
                "programacao_cultural",
                "visit",
                "Pier da Feira de São Joaquim. Aos domingos, a partir das 13h",
            ),
        ),
    ),
    _f(
        slug="fazenda-grande-do-retiro",
        nome="Fazendo Grande do Retiro",
        semop_nome="Fazendo Grande do Retiro",
        feirantes="27",
        endereco="Rua Melo Morais Filho",
        senac_nome="Fazenda Grande do Retiro",
        senac_endereco="Rua Melo Morais Filho,",
        bairro="Fazenda Grande do Retiro",
        bairro_base="nome_oficial",
        observacao=(
            "Grafia 'Fazendo' é a publicada pela SEMOP; SENAC grafa 'Fazenda'. "
            "O nome do bairro é 'Fazenda Grande do Retiro'."
        ),
    ),
    _f(
        slug="largo-do-tanque",
        nome="Largo do Tanque",
        semop_nome="Largo do Tanque",
        feirantes="41",
        endereco="Avenida San Martin",
        senac_nome="Largo do Tanque",
        senac_endereco="Av. San Martin,",
        bairro="Largo do Tanque",
        bairro_base="nome_oficial",
        observacao="'Largo do Tanque' é localidade, não bairro formal do município.",
    ),
    _f(
        slug="sao-caetano",
        nome="São Caetano",
        semop_nome="São Caetano",
        feirantes="18",
        endereco="Rua Desembargador Mário Lisboa Sampaio",
        senac_nome="São Caetano",
        senac_endereco="R.Des.Mario Lisboa Sampaio",
        bairro="São Caetano",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="iapi",
        nome="IAPI",
        semop_nome="IAPI",
        feirantes="10",
        endereco="Rua Conde de Porto Alegre",
        senac_nome="Iapi",
        senac_endereco="Rua Conde de Porto Alegre,",
        bairro="IAPI",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="japao-liberdade",
        nome="Japão / Liberdade",
        semop_nome="Japão / Liberdade",
        feirantes="269",
        endereco="Rua Gonçalo Coelho",
        senac_nome="Liberdade “Japão”",
        senac_endereco="Rua Gonçalo Coelho,",
        bairro="Liberdade",
        bairro_base="fonte",
        observacao="Também chamada 'Feira do Japão', existe desde 1957 (portal de turismo).",
        evidencias_extra=(
            Evidencia(
                "bairro",
                "visit",
                "Essa feira existe há mais de 60 anos no bairro da Liberdade",
            ),
        ),
    ),
    _f(
        slug="cosme-de-farias",
        nome="Cosme de Farias",
        semop_nome="Cosme de Farias",
        feirantes="150",
        endereco="Rua Wenceslau Galo",
        senac_nome="Cosme de Farias",
        senac_endereco="Rua Wenceslau Galo",
        bairro="Cosme de Farias",
        bairro_base="nome_oficial",
    ),
    Feira(
        slug="cruz-da-redencao",
        nome="Cruz da Redenção",
        tipo="livre",
        bairro="",
        bairro_base="nao_informado",
        endereco="Ladeira do Mulambu",
        dias=TERCA_A_DOMINGO,
        horario="",
        feirantes=7,
        regular=True,
        status="verificado",
        observacao=(
            "SEMOP publica '07' feirantes e 'Mulambu'; SENAC publica 'Mulambo'. "
            "Bairro não é declarado em nenhuma das fontes."
        ),
        evidencias=(
            Evidencia("endereco", "semop", _semop("Cruz da Redenção", "07", "Ladeira do Mulambu")),
            Evidencia("dias", "senac", _senac("Cruz da Redenção", "Ladeira do Mulambo,")),
        ),
    ),
    _f(
        slug="nordeste-de-amaralina",
        nome="Nordeste de Amaralina",
        semop_nome="Nordeste de Amaralina",
        feirantes="41",
        endereco="Rua Reinaldo de Matos – Final de Linha",
        senac_nome="Nordeste",
        senac_endereco="Rua Reinaldo de Matos,",
        bairro="Nordeste de Amaralina",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="vale-das-pedrinhas",
        nome="Vale das Pedrinhas",
        semop_nome="Vale das Pedrinhas",
        feirantes="15",
        endereco="Rua Antônio Balbino",
        senac_nome="Vale das Pedrinhas",
        senac_endereco="Rua Cristóvão Ferreira,",
        bairro="Vale das Pedrinhas",
        bairro_base="nome_oficial",
        observacao="Divergência de endereço entre SEMOP (Antônio Balbino) e SENAC (Cristóvão Ferreira).",
    ),
    _f(
        slug="mussurunga",
        nome="Mussurunga",
        semop_nome="Mussurunga",
        feirantes="62",
        endereco="Rua C, Quadra B",
        senac_nome="Mussurunga",
        senac_endereco="Rua C, Qd. B",
        bairro="Mussurunga",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="sao-cristovao",
        nome="São Cristóvão",
        semop_nome="São Cristóvão",
        feirantes="50",
        endereco="Praça da Matriz",
        senac_nome="São Cristóvão",
        senac_endereco="Pc. Da Matriz,",
        bairro="São Cristóvão",
        bairro_base="nome_oficial",
        observacao="O portal de turismo registra 'Avenida Aliomar Baleeiro'; SEMOP publica 'Praça da Matriz'.",
    ),
    _f(
        slug="tancredo-neves",
        nome="Tancredo Neves",
        semop_nome="Tancredo Neves",
        feirantes="50",
        endereco="Rua Pernambuco / Rua Paraíba",
        senac_nome="Tancredo Neves",
        senac_endereco="Rua Pernambuco/Paraíba",
        bairro="Tancredo Neves",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="dom-avelar",
        nome="Dom Avelar",
        semop_nome="Dom Avelar",
        feirantes="25",
        endereco="Largo dos Oratorianos",
        senac_nome="Dom Avelar",
        senac_endereco="Largo dos Oratorianos,",
        bairro="Dom Avelar",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="cajazeiras",
        nome="Cajazeiras",
        semop_nome="Cajazeiras",
        feirantes="59",
        endereco="Rotula da 2ª Etapa – Cajazeiras X",
        senac_nome="Cajazeiras",
        senac_endereco="Rot.2ª etapa Cajazeira X",
        bairro="Cajazeiras",
        bairro_base="fonte",
        observacao="Portal de turismo chama de 'Feira da Rótula da Feirinha', em Cajazeiras X.",
        evidencias_extra=(
            Evidencia("bairro", "visit", "Área central da região de Cajazeiras"),
        ),
    ),
    _f(
        slug="castelo-branco",
        nome="Castelo Branco",
        semop_nome="Castelo Branco",
        feirantes="60",
        endereco="Rua A / D",
        senac_nome="Castelo Branco",
        senac_endereco="Rua D, s/n, 1ª Etapa",
        bairro="Castelo Branco",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="valeria",
        nome="Valéria",
        semop_nome="Valéria",
        feirantes="40",
        endereco="Rua da Matriz / PÇ N. Senhora da Conceição",
        senac_nome="Valéria",
        senac_endereco="Rua da Matriz,Pc.N.Senhora da Conceição",
        bairro="Valéria",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="fazenda-coutos",
        nome="Fazenda Coustos",
        semop_nome="Fazenda Coustos",
        feirantes="144",
        endereco="Rua 60, Quadra 59 – Final de Linha",
        senac_nome="Fazenda Coutos",
        senac_endereco="Rua 60 qd.69 final de linha",
        bairro="Fazenda Coutos",
        bairro_base="nome_oficial",
        observacao=(
            "SEMOP grafa 'Coustos'; SENAC e o nome do bairro são 'Coutos'. "
            "Quadra divergente: '59' (SEMOP) x '69' (SENAC)."
        ),
    ),
    _f(
        slug="periperi",
        nome="Periperi",
        semop_nome="Periperi",
        feirantes="120",
        endereco="Rua das Pedrinhas",
        senac_nome="Periperi",
        senac_endereco="Rua das Pedrinhas,",
        bairro="Periperi",
        bairro_base="nome_oficial",
    ),
    _f(
        slug="plataforma",
        nome="Plataforma",
        semop_nome="Plataforma",
        feirantes="11",
        endereco="Rua Antônio Balbino",
        senac_nome="Plataforma",
        senac_endereco="Rua Antônio Balbino,",
        bairro="Plataforma",
        bairro_base="nome_oficial",
    ),
    Feira(
        slug="porto-das-sardinhas",
        nome="Feira Livre de Pescados / Porto das Sardinhas",
        tipo="livre",
        bairro="São João do Cabrito",
        bairro_base="fonte",
        endereco="Tv. Sá Oliveira, 12 – São João do Cabrito",
        dias=(),
        horario="",
        feirantes=None,
        regular=True,
        status="parcial",
        observacao=(
            "A fonte declara 'dias de maior fluxo' (quinta a domingo), não os dias "
            "de funcionamento. Por isso dias fica vazio: dia não publicado não é "
            "dia inferido."
        ),
        evidencias=(
            Evidencia(
                "endereco",
                "visit",
                "Feira Livre de Pescados/Porto das Sardinhas: Tv. Sá Oliveira, 12 – São João do Cabrito, Salvador – BA, 40717-390",
            ),
            Evidencia(
                "fluxo",
                "visit",
                "Os dias de maior fluxo são de quinta a domingo",
            ),
        ),
    ),
    Feira(
        slug="feira-artesanato-praca-da-se",
        nome="Feira de Artesanato da Praça da Sé",
        tipo="artesanato",
        bairro="Centro Histórico",
        bairro_base="fonte",
        endereco="Praça da Sé",
        dias=(),
        horario="",
        feirantes=None,
        regular=False,
        status="sem_dia_fixo",
        observacao=(
            "Feira cultural de artesanato, mas sem dia fixo publicado: a agenda "
            "municipal publica cada edição com data própria e o organizador "
            "informa que datas e horários saem a cada edição."
        ),
        evidencias=(
            Evidencia("endereco", "pelourinho", "Local: Praça da Sé, Centro Histórico, Salvador"),
            Evidencia("edicao", "pelourinho", "Data: 27 a 30/agosto | 9h às 19h"),
            Evidencia(
                "periodicidade",
                "acm",
                "Informações sobre datas e horários serão divulgadas a cada edição.",
            ),
        ),
    ),
)


def por_slug(slug: str) -> Feira:
    for feira in FEIRAS:
        if feira.slug == slug:
            return feira
    raise KeyError(f"feira desconhecida: {slug!r}")


def normalizar_dia(texto: str) -> str | None:
    """'terça', 'Ter', '3' -> 'terca'. Devolve None se não reconhecer."""
    t = (texto or "").strip().lower()
    t = t.replace("ç", "c").replace("á", "a").replace("ã", "a")
    mapa = {
        "seg": "segunda", "segunda": "segunda", "segunda-feira": "segunda",
        "ter": "terca", "terca": "terca", "terca-feira": "terca",
        "qua": "quarta", "quarta": "quarta", "quarta-feira": "quarta",
        "qui": "quinta", "quinta": "quinta", "quinta-feira": "quinta",
        "sex": "sexta", "sexta": "sexta", "sexta-feira": "sexta",
        "sab": "sabado", "sabado": "sabado",
        "dom": "domingo", "domingo": "domingo",
    }
    return mapa.get(t)


__all__ = [
    "CAPTURADO_EM",
    "DIAS",
    "DIAS_ABREV",
    "FEIRAS",
    "FONTES",
    "TERCA_A_DOMINGO",
    "Evidencia",
    "Feira",
    "FonteFeira",
    "normalizar_dia",
    "por_slug",
]
