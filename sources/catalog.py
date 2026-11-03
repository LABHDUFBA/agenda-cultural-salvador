"""Catálogo de fontes oficiais da agenda cultural de Salvador (BA).

Este pacote é a única fonte de verdade sobre *de onde* o projeto coleta.
Cada fonte declara a URL canônica, o escopo administrativo, o tipo de
conteúdo publicado e o coletor responsável. O coletor nunca escolhe a URL
sozinho: ele recebe uma :class:`FonteCultural` daqui.

Regras do catálogo (verificadas em ``tests/test_collectors.py``,
``tests/test_plataformas_ingressos.py`` e ``tests/test_coletores_ufba.py``):

* exatamente ``TOTAL_FONTES`` (31) entradas, com ``id`` e ``url`` únicos:
  as 13 fontes institucionais originais + as 13 plataformas de ingresso
  obrigatórias do projeto + 5 fontes UFBA/independentes sondadas em
  2026-11-03 (``docs/coletores-ufba.md``);
* somente ``https``;
* ``coletor`` precisa existir no registro de coletores;
* ``acessibilidade`` registra o resultado **real** da última sondagem, não
  uma expectativa. Fonte bloqueada continua no catálogo — bloqueio é dado,
  não motivo para remover a fonte nem para tentar contorná-lo.

Semântica de ``acessibilidade`` (importante para não mentir sobre cobertura):

* ``ok`` — alcançável **e** existe coletor que extrai eventos dela;
* ``nao_suportada`` — alcançável (o ``http_status`` real fica registrado),
  mas o projeto ainda não tem parser para o formato. Não coleta, não finge;
* ``bloqueada`` — 401/403/429 ou robots.txt vedando; sem retry nem disfarce;
* ``indisponivel`` — DNS/timeout/5xx no momento da sondagem;
* ``nao_verificada`` — nunca sondada.

As 13 plataformas de ingresso abaixo foram sondadas em 2026-11-03 com o
``HttpFetcher`` do projeto (UA identificado, robots.txt respeitado, uma
requisição por fonte). O resultado bruto está em
``sources/probe_report_plataformas.json``. Três delas passaram a ter coletor
verificado com coleta real no mesmo dia — ``sympla_salvador``,
``casa_rosa`` e ``bilheteria_digital_salvador`` (``docs/coletores-plataformas.md``);
as outras 10 seguem sem parser e por isso não são ``coletavel``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Escopo = Literal["municipal", "estadual", "federal", "privado"]
TipoConteudo = Literal[
    "calendario_json",   # calendário renderizado com JSON embutido na página
    "cartoes_html",      # listagem HTML com cartões e data no cabeçalho
    "agenda_texto",      # agenda em texto corrido (baixa estrutura)
    "agenda_html",       # listagem HTML genérica
    "app_javascript",    # conteúdo só existe após execução de JS
    "portal",            # portal institucional sem agenda própria
]
Acessibilidade = Literal["ok", "bloqueada", "indisponivel", "nao_suportada", "nao_verificada"]

TOTAL_FONTES = 31  # 13 institucionais + 13 plataformas + 5 UFBA/independentes


@dataclass(frozen=True, slots=True)
class FonteCultural:
    """Uma fonte oficial de programação cultural em Salvador."""

    id: str
    nome: str
    organizacao: str
    url: str
    escopo: Escopo
    tipo_conteudo: TipoConteudo
    coletor: str
    confiabilidade: Literal["alta", "media", "baixa"] = "media"
    # Resultado da sondagem real (ver sources/README.md e collectors.cli probe).
    acessibilidade: Acessibilidade = "nao_verificada"
    http_status: int | None = None
    observacao: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def coletavel(self) -> bool:
        """Só fontes acessíveis e com estrutura suportada entram na coleta."""
        return self.acessibilidade == "ok" and self.tipo_conteudo != "app_javascript"


CATALOGO: tuple[FonteCultural, ...] = (
    FonteCultural(
        id="pelourinho_dia_noite",
        nome="Pelourinho Dia e Noite — Agenda Completa",
        organizacao="SECULT / Prefeitura Municipal de Salvador",
        url="https://pelourinhodiaenoite.salvador.ba.gov.br/agenda-completa/",
        escopo="municipal",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="alta",
        acessibilidade="bloqueada",
        observacao=(
            "DNS resolve (177.20.74.10) mas o TCP não completa a partir desta "
            "rede: timeout de 25 s sem SYN-ACK. Bloqueio de rede, não de "
            "conteúdo. Nenhuma tentativa de contorno (proxy/espelho) é feita."
        ),
        tags=("centro-historico", "municipal", "agenda"),
    ),
    FonteCultural(
        id="secult_salvador",
        nome="SECULT Salvador — Secretaria de Cultura e Turismo",
        organizacao="Prefeitura Municipal de Salvador",
        url="https://secult.salvador.ba.gov.br/",
        escopo="municipal",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="alta",
        acessibilidade="bloqueada",
        observacao=(
            "Mesma faixa 177.20.74.0/24 do Pelourinho: TCP filtrado a partir "
            "desta rede. Conteúdo visto apenas por índice de terceiros; não é "
            "usado como substituto da fonte."
        ),
        tags=("municipal", "noticias", "agenda"),
    ),
    FonteCultural(
        id="prefeitura_salvador",
        nome="Prefeitura de Salvador — Portal",
        organizacao="Prefeitura Municipal de Salvador",
        url="https://www.salvador.ba.gov.br/",
        escopo="municipal",
        tipo_conteudo="portal",
        coletor="bloqueada",
        confiabilidade="alta",
        acessibilidade="bloqueada",
        observacao=(
            "Host 177.20.65.15: TCP filtrado a partir desta rede (timeout). "
            "Sem coleta até haver rota legítima de rede (ex.: execução no "
            "próprio datacenter municipal)."
        ),
        tags=("municipal", "portal"),
    ),
    FonteCultural(
        id="tca",
        nome="Teatro Castro Alves — Calendário de Eventos",
        organizacao="Teatro Castro Alves / SECULT-BA",
        url="https://www.ba.gov.br/tca/eventos",
        escopo="estadual",
        tipo_conteudo="calendario_json",
        coletor="bagov_calendario",
        confiabilidade="alta",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "Drupal 10. O FullCalendar recebe os eventos embutidos em "
            "drupalSettings (title, url, start, end): extração determinística, "
            "sem executar JS. 171 eventos na sondagem de 2026-11-03."
        ),
        tags=("teatro", "musica", "estadual", "sala-principal"),
    ),
    FonteCultural(
        id="funceb",
        nome="FUNCEB — Calendário de Eventos",
        organizacao="Fundação Cultural do Estado da Bahia / SECULT-BA",
        url="https://www.ba.gov.br/fundacaocultural/eventos",
        escopo="estadual",
        tipo_conteudo="calendario_json",
        coletor="bagov_calendario",
        confiabilidade="alta",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "Mesmo tema Drupal do TCA; calendário com 1 evento na sondagem. "
            "A fundação publica a maior parte da programação em notícias, que "
            "não são tratadas aqui como evento."
        ),
        tags=("formacao", "editais", "estadual"),
    ),
    FonteCultural(
        id="secult_ba",
        nome="SECULT-BA — Eventos",
        organizacao="Secretaria de Cultura do Estado da Bahia",
        url="https://www.ba.gov.br/cultura/eventos",
        escopo="estadual",
        tipo_conteudo="calendario_json",
        coletor="bagov_calendario",
        confiabilidade="alta",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "Calendário existe e responde 200, mas veio vazio (0 eventos) na "
            "sondagem. Coleta válida com resultado zero — não é falha."
        ),
        tags=("estadual", "politica-cultural"),
    ),
    FonteCultural(
        id="ipac",
        nome="IPAC — Eventos",
        organizacao="Instituto do Patrimônio Artístico e Cultural da Bahia",
        url="https://www.ba.gov.br/ipac/eventos",
        escopo="estadual",
        tipo_conteudo="calendario_json",
        coletor="bagov_calendario",
        confiabilidade="alta",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "Calendário responde 200 e veio vazio na sondagem. Museus do IPAC "
            "divulgam programação em notícias; não convertidas em evento."
        ),
        tags=("museus", "patrimonio", "estadual"),
    ),
    FonteCultural(
        id="teatro_vila_velha",
        nome="Teatro Vila Velha — Agenda Mensal",
        organizacao="Teatro Vila Velha (organização social de cultura)",
        url="https://teatrovilavelha.com.br/tempo/",
        escopo="privado",
        tipo_conteudo="agenda_texto",
        coletor="vila_velha_texto",
        confiabilidade="media",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "Agenda publicada como post WordPress em texto corrido "
            "('TÍTULO - 12 de agosto - 19h - LOCAL'). Sem hora de término e "
            "com ano implícito no cabeçalho do mês (ex.: 'agosto/26'). Toda "
            "coleta daqui entra como baixa confiança e revisão obrigatória."
        ),
        tags=("teatro", "danca", "privado"),
    ),
    FonteCultural(
        id="salvador_da_bahia",
        nome="Salvador da Bahia — Agenda do dia",
        organizacao="Salvador da Bahia (portal oficial de turismo)",
        url="https://www.salvadordabahia.com/agenda/",
        escopo="municipal",
        tipo_conteudo="cartoes_html",
        coletor="salvador_da_bahia_cartoes",
        confiabilidade="media",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "WordPress + Yoast. Cartões com data no cabeçalho do dia "
            "('03 de Novembro de 2026') e hora não publicada: o evento entra "
            "à meia-noite (America/Bahia) com revisão obrigatória."
        ),
        tags=("turismo", "municipal", "agenda"),
    ),
    FonteCultural(
        id="caixa_cultural_salvador",
        nome="Caixa Cultural Salvador — Programação",
        organizacao="Caixa Econômica Federal",
        url="https://www.caixacultural.gov.br/",
        escopo="federal",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="alta",
        acessibilidade="bloqueada",
        http_status=403,
        observacao=(
            "HTTP 403 imediato para o agente identificado (WAF). Sem retry "
            "com outro User-Agent, sem navegador headless: bloqueio respeitado."
        ),
        tags=("exposicoes", "federal", "centro-cultural"),
    ),
    FonteCultural(
        id="mam_bahia",
        nome="MAM Bahia — Museu de Arte Moderna da Bahia",
        organizacao="Museu de Arte Moderna da Bahia (Solar do Unhão)",
        url="https://www.mam.ba.gov.br/",
        escopo="estadual",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="alta",
        acessibilidade="indisponivel",
        observacao=(
            "DNS não resolve (NXDOMAIN) e conexão não abre. Domínio "
            "institucional sem registro ativo; registrar e monitorar."
        ),
        tags=("artes-visuais", "museu", "estadual"),
    ),
    FonteCultural(
        id="muncab",
        nome="MUNCAB — Museu Nacional da Cultura Afro-Brasileira",
        organizacao="MUNCAB (associação civil, Centro Histórico)",
        url="https://www.muncab.com.br/",
        escopo="privado",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="media",
        acessibilidade="bloqueada",
        http_status=403,
        observacao=(
            "HTTP 403 para requisição identificada. Bloqueio respeitado; "
            "sem tentativa de contorno."
        ),
        tags=("museu", "afro-brasileiro", "privado"),
    ),
    FonteCultural(
        id="sesc_bahia",
        nome="SESC Bahia — Programação cultural",
        organizacao="SESC Bahia (Sistema S)",
        url="https://www.sescbahia.com.br/programacao",
        escopo="privado",
        tipo_conteudo="app_javascript",
        coletor="nao_suportada",
        confiabilidade="media",
        acessibilidade="nao_suportada",
        http_status=200,
        observacao=(
            "SPA Angular: o HTML entregue tem 10 KB e nenhum evento; os dados "
            "vêm de uma API interna não documentada "
            "(sescbahia.azurewebsites.net/gerenciador/api). Não fazemos "
            "engenharia reversa de API privada: fonte fica registrada e "
            "pendente de API pública/calendário servido no HTML."
        ),
        tags=("teatro", "musica", "sistema-s"),
    ),
    # ------------------------------------------------------------------
    # As 13 plataformas de ingresso obrigatórias do projeto.
    #
    # Todas são sondadas com o mesmo cliente HTTP do resto do catálogo.
    # Nenhuma tem coletor implementado: as alcançáveis entram como
    # ``nao_suportada`` (http_status real preservado) e as que recusam
    # acesso entram como ``bloqueada``/``indisponivel``. Assim o catálogo
    # cobre 100% das fontes obrigatórias sem alegar coleta que não existe.
    # ------------------------------------------------------------------
    FonteCultural(
        id="shotgun_salvador",
        nome="Shotgun — Eventos em Salvador",
        organizacao="Shotgun Live",
        url="https://shotgun.live/pt-br/cities/salvador",
        escopo="privado",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="media",
        acessibilidade="bloqueada",
        http_status=429,
        observacao=(
            "HTTP 429 (rate limit) para o agente identificado em 2026-11-03, "
            "sem retry e sem troca de User-Agent. O HTML não foi inspecionado: "
            "tipo_conteudo é registro provisório, não observação."
        ),
        tags=("ingressos", "marketplace", "salvador"),
    ),
    FonteCultural(
        id="shotgun_manifesto",
        nome="Shotgun — Manifesto (venue)",
        organizacao="Shotgun Live / Manifesto",
        url="https://shotgun.live/pt-br/venues/manifesto",
        escopo="privado",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="media",
        acessibilidade="bloqueada",
        http_status=429,
        observacao=(
            "Mesmo host do shotgun_salvador: HTTP 429 em 2026-11-03, sem "
            "retry. Página de venue, conteúdo não inspecionado."
        ),
        tags=("ingressos", "venue", "manifesto"),
    ),
    FonteCultural(
        id="ingresse",
        nome="Ingresse — Portal de ingressos",
        organizacao="Ingresse",
        url="https://ingresse.com/",
        escopo="privado",
        tipo_conteudo="portal",
        coletor="bloqueada",
        confiabilidade="media",
        acessibilidade="bloqueada",
        http_status=403,
        observacao=(
            "HTTP 403 imediato para o agente identificado em 2026-11-03 "
            "(WAF/CDN). Bloqueio respeitado: sem retry, sem proxy, sem "
            "navegador headless."
        ),
        tags=("ingressos", "marketplace"),
    ),
    FonteCultural(
        id="sympla_salvador",
        nome="Sympla — Eventos em Salvador (BA)",
        organizacao="Sympla",
        url="https://www.sympla.com.br/eventos/salvador-ba",
        escopo="privado",
        tipo_conteudo="cartoes_html",
        coletor="sympla_payload",
        confiabilidade="alta",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "HTTP 200, 428.095 bytes, robots.txt permitido (2026-11-03). "
            "Coletor `sympla_payload` lê o JSON de hidratação que a própria "
            "página publica (título, URL canônica, start_date/end_date ISO "
            "com offset, local, endereço, cidade/UF, coordenadas): 45 "
            "objetos, 31 eventos únicos por URL, todos com cidade Salvador. "
            "Uma requisição, sem API privada e sem JS. Datas convertidas de "
            "UTC para America/Bahia (conversão, não inferência)."
        ),
        tags=("ingressos", "marketplace", "salvador"),
    ),
    FonteCultural(
        id="casa_rosa",
        nome="Casa Rosa — Site oficial",
        organizacao="Casa Rosa (Rio Vermelho) / Bileto-Sympla",
        url="https://site.bileto.sympla.com.br/casarosa/",
        escopo="privado",
        tipo_conteudo="cartoes_html",
        coletor="casa_rosa_widget",
        confiabilidade="media",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "HTTP 200, 10.857 bytes, robots.txt ausente (RFC 9309: "
            "permitido), 2026-11-03. A página institucional não lista evento: "
            "publica um iframe público do Sympla "
            "(`sympla.com.br/agenda-eventos/<token>?theme=light`). O coletor "
            "`casa_rosa_widget` segue esse iframe (segunda URL publicada pela "
            "própria página; 23.262 bytes, HTTP 200, robots permitido) e lê os "
            "cartões server-rendered: 5 eventos, nome completo em "
            "`data-name`, dia/mês/hora e local 'Casa Rosa - BA'. O ano não é "
            "publicado — é inferido e declarado em `campos_inferidos`."
        ),
        tags=("venue", "rio-vermelho", "portal"),
    ),
    FonteCultural(
        id="eventim_salvador",
        nome="Eventim — Salvador",
        organizacao="Eventim Brasil",
        url="https://www.eventim.com.br/city/salvador-1143/",
        escopo="privado",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="media",
        acessibilidade="indisponivel",
        observacao=(
            "Conexão TCP estabelecida, mas a leitura estourou 15 s em "
            "2026-11-03 (read timeout). Sem retry nesta sondagem; "
            "tipo_conteudo é registro provisório, não observação."
        ),
        tags=("ingressos", "marketplace", "salvador"),
    ),
    FonteCultural(
        id="bilheteria_digital_salvador",
        nome="Bilheteria Digital — Busca Salvador",
        organizacao="Bilheteria Digital",
        url="https://www.bilheteriadigital.com/busca/salvador",
        escopo="privado",
        tipo_conteudo="cartoes_html",
        coletor="bilheteria_digital_cartoes",
        confiabilidade="media",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "HTTP 200, 120.974 bytes, robots.txt permitido (2026-11-03). Os "
            "resultados da busca **estão** no HTML entregue (9 cartões "
            "`li.box-li-evento`; retificação da observação anterior, que "
            "contava só links absolutos). O coletor "
            "`bilheteria_digital_cartoes` mantém os 8 cartões de Salvador/BA "
            "(1 de Ituberá é descartado, não reinterpretado) e lê a página "
            "de detalhe publicada no cartão para obter `dataInicio` ISO "
            "(UTC) exato e o preço; nesses casos a confiança é alta. Cartão "
            "sem detalhe cai para dia/mês com ano e hora inferidos."
        ),
        tags=("ingressos", "busca", "salvador"),
    ),
    FonteCultural(
        id="ticketmaker",
        nome="Ticketmaker — Portal",
        organizacao="Ticketmaker",
        url="https://www.ticketmaker.com.br/",
        escopo="privado",
        tipo_conteudo="cartoes_html",
        coletor="nao_suportada",
        confiabilidade="media",
        acessibilidade="nao_suportada",
        http_status=200,
        observacao=(
            "HTTP 200, 973.329 bytes, robots.txt permitido (2026-11-03). "
            "Listagem com 735 links de evento no HTML, abrangência nacional "
            "— exigiria filtro por cidade antes de qualquer coleta. Sem "
            "coletor implementado."
        ),
        tags=("ingressos", "marketplace", "nacional"),
    ),
    FonteCultural(
        id="ticketmaker_eventos",
        nome="Ticketmaker — Todos os eventos (vendas)",
        organizacao="Ticketmaker",
        url="https://vendas.ticketmaker.com.br/Eventos",
        escopo="privado",
        tipo_conteudo="cartoes_html",
        coletor="nao_suportada",
        confiabilidade="media",
        acessibilidade="nao_suportada",
        http_status=200,
        observacao=(
            "HTTP 200, 221.647 bytes, robots.txt permitido (2026-11-03). "
            "138 links de evento no HTML; sem filtro de cidade na URL. "
            "Sem coletor implementado."
        ),
        tags=("ingressos", "marketplace", "nacional"),
    ),
    FonteCultural(
        id="ticketmaster_brasil",
        nome="Ticketmaster Brasil",
        organizacao="Ticketmaster Brasil",
        url="https://www.ticketmaster.com.br/",
        escopo="privado",
        tipo_conteudo="portal",
        coletor="bloqueada",
        confiabilidade="media",
        acessibilidade="bloqueada",
        http_status=403,
        observacao=(
            "HTTP 403 imediato para o agente identificado em 2026-11-03 "
            "(proteção de bot). Bloqueio respeitado; sem tentativa de "
            "contorno."
        ),
        tags=("ingressos", "marketplace", "nacional"),
    ),
    FonteCultural(
        id="bora_tickets",
        nome="Bora Tickets — Eventos",
        organizacao="Bora Tickets",
        url="https://vendas.boratickets.com.br/",
        escopo="privado",
        tipo_conteudo="agenda_html",
        coletor="nao_suportada",
        confiabilidade="media",
        acessibilidade="nao_suportada",
        http_status=200,
        observacao=(
            "HTTP 200, 56.768 bytes, robots.txt permitido (2026-11-03). "
            "Next.js com __NEXT_DATA__ e 23 links de evento no HTML; "
            "abrangência nacional, sem filtro de cidade. Sem coletor."
        ),
        tags=("ingressos", "marketplace", "nacional"),
    ),
    FonteCultural(
        id="pida",
        nome="Pida — Agenda de eventos",
        organizacao="Pida.com.br",
        url="https://www.pida.com.br/eventos",
        escopo="privado",
        tipo_conteudo="cartoes_html",
        coletor="nao_suportada",
        confiabilidade="media",
        acessibilidade="nao_suportada",
        http_status=200,
        observacao=(
            "HTTP 200, 1.311.237 bytes, robots.txt permitido (2026-11-03). "
            "Agenda com 43 links de evento no HTML, abrangência nacional; "
            "sem coletor implementado."
        ),
        tags=("ingressos", "marketplace", "nacional"),
    ),
    FonteCultural(
        id="nokta_salvador",
        nome="Nokta — Eventos em Salvador",
        organizacao="Nokta Tickets",
        url="https://www.noktatickets.com.br/eventos/em/salvador",
        escopo="privado",
        tipo_conteudo="cartoes_html",
        coletor="nao_suportada",
        confiabilidade="media",
        acessibilidade="nao_suportada",
        http_status=200,
        observacao=(
            "HTTP 200, 619.886 bytes, robots.txt permitido (2026-11-03). "
            "Listagem por cidade (Salvador) com 17 links de evento no HTML "
            "entregue; Next.js. Sem coletor implementado — nenhum evento "
            "extraído."
        ),
        tags=("ingressos", "marketplace", "salvador"),
    ),
    # ------------------------------------------------------------------
    # Fontes UFBA e independentes acrescentadas em 2026-11-03.
    #
    # Mesmo cliente HTTP, mesma regra: o status registrado é o da sondagem
    # real; fonte sem parser fica ``nao_suportada`` e fonte inalcançável
    # fica ``indisponivel`` — nenhuma das duas é maquiada como cobertura.
    # ------------------------------------------------------------------
    FonteCultural(
        id="ufba_agenda",
        nome="Agenda UFBA — Portal de eventos",
        organizacao="Universidade Federal da Bahia",
        url="https://ufba.br/agenda-ufba",
        escopo="federal",
        tipo_conteudo="cartoes_html",
        coletor="ufba_agenda_tabela",
        confiabilidade="alta",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "HTTP 200 (~48 KB, robots.txt `Allow: /`) em 2026-11-03. "
            "View Drupal renderizada no servidor em `table.views-table`: "
            "categoria, título/URL, data (`dd/mm/aaaa - HhMM`, às vezes com "
            "fim), local e descrição em parágrafos com classe própria. O "
            "coletor `ufba_agenda_tabela` extrai 8 linhas, todas futuras ou "
            "em curso na sondagem. A hora é a publicada pelo editor (inclusive "
            "valores de preenchimento como 8h08); nada é arredondado."
        ),
        tags=("ufba", "federal", "agenda", "universidade"),
    ),
    FonteCultural(
        id="ufba_escola_teatro",
        nome="Escola de Teatro da UFBA — Agenda",
        organizacao="Escola de Teatro / Universidade Federal da Bahia",
        url="https://teatro.ufba.br/agenda/",
        escopo="federal",
        tipo_conteudo="calendario_json",
        coletor="ufba_teatro_json",
        confiabilidade="alta",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "HTTP 200, 366.161 bytes, robots.txt `Allow: /` (2026-11-03). "
            "Next.js: `props.pageProps.agenda` traz uid, título, subtítulo, "
            "local, tipo e `date` ISO com offset. O coletor "
            "`ufba_teatro_json` lê 63 registros e converte UTC para "
            "America/Bahia. Na sondagem o registro mais recente era "
            "31/03/2026: **não havia evento futuro publicado**, então a "
            "cobertura ativa hoje é zero — ausência na fonte, não falha."
        ),
        tags=("ufba", "teatro", "federal", "escola-de-teatro"),
    ),
    FonteCultural(
        id="cine_glauber_rocha",
        nome="Cine Glauber Rocha — Programação",
        organizacao="Cine Glauber Rocha / Universidade Federal da Bahia",
        url="https://www.cineglauberrocha.com.br/glauber-rocha/programacao",
        escopo="federal",
        tipo_conteudo="cartoes_html",
        coletor="cine_glauber_sessoes",
        confiabilidade="alta",
        acessibilidade="ok",
        http_status=200,
        observacao=(
            "HTTP 200, 299.504 bytes, robots.txt `Allow` (Disallow só para "
            "lightbox/admin) em 2026-11-03. HTML renderizado no servidor: 48 "
            "`div.filme`, 57 `div.sala-filme`. Cada sessão publica a frase "
            "acessível 'ingresso para <filme>, sessão de dd/mm/aaaa às HH:MM, "
            "sala N' — dela saem data, hora e sala, sem inferência. O link "
            "de compra (tikt.com.br) entra como `url_ingresso`; a página do "
            "filme é a proveniência."
        ),
        tags=("ufba", "cinema", "federal", "sessoes"),
    ),
    FonteCultural(
        id="agenda_ufba_portal",
        nome="Agenda UFBA — Portal de eventos (agenda.ufba.br)",
        organizacao="Universidade Federal da Bahia",
        url="https://agenda.ufba.br/",
        escopo="federal",
        tipo_conteudo="agenda_html",
        coletor="bloqueada",
        confiabilidade="alta",
        acessibilidade="indisponivel",
        observacao=(
            "Certificado válido para `*.agenda.ufba.br` (GlobalSign) mas o "
            "servidor não envia o intermediário `GlobalSign RSA OV SSL CA "
            "2018`: a verificação falha com 'unable to verify the first "
            "certificate' (openssl, 2026-11-03). O projeto não desativa "
            "verificação de TLS nem fixa CA no código; a fonte fica "
            "registrada como indisponível até o servidor corrigir a cadeia. "
            "O conteúdo equivalente é coletado de `ufba.br/agenda-ufba`."
        ),
        tags=("ufba", "federal", "agenda", "tls"),
    ),
    FonteCultural(
        id="agenda_arte_cultura",
        nome="Agenda Arte e Cultura — agenda da Universidade e de Salvador",
        organizacao="Agenda Arte e Cultura (publicação independente)",
        url="https://www.agendartecultura.com.br/",
        escopo="privado",
        tipo_conteudo="agenda_html",
        coletor="nao_suportada",
        confiabilidade="media",
        acessibilidade="nao_suportada",
        http_status=200,
        observacao=(
            "HTTP 200, 117.964 bytes, robots.txt permite (só /cgi-bin e "
            "/wusage vedados) em 2026-11-03. WordPress com matérias por "
            "linguagem (artes visuais, audiovisual, dança, música, teatro) e "
            "menu de equipamentos culturais da UFBA. **Não** publica data de "
            "evento em campo estruturado: as tags <time> trazem a data de "
            "publicação da matéria e a data do evento só aparece no texto "
            "corrido. Sem parser: converter publicação em data de evento "
            "seria inventar. Fonte útil para pauta, não para coleta."
        ),
        tags=("ufba", "independente", "agenda", "divulgacao"),
    ),
)


def por_id(fonte_id: str) -> FonteCultural:
    """Devolve a fonte do catálogo ou levanta ``KeyError`` explicativo."""
    for fonte in CATALOGO:
        if fonte.id == fonte_id:
            return fonte
    ids = ", ".join(f.id for f in CATALOGO)
    raise KeyError(f"fonte desconhecida: {fonte_id!r} (disponíveis: {ids})")


def fontes_coletaveis() -> tuple[FonteCultural, ...]:
    return tuple(f for f in CATALOGO if f.coletavel)


def resumo() -> dict[str, int]:
    """Contagem por acessibilidade, para relatórios e health checks."""
    contagem: dict[str, int] = {}
    for fonte in CATALOGO:
        contagem[fonte.acessibilidade] = contagem.get(fonte.acessibilidade, 0) + 1
    return contagem


__all__ = [
    "CATALOGO",
    "TOTAL_FONTES",
    "FonteCultural",
    "fontes_coletaveis",
    "por_id",
    "resumo",
]
