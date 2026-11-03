# Fontes oficiais da agenda cultural de Salvador

Catálogo e coletores iniciais. Este diretório é a **única fonte de verdade**
sobre de onde o projeto coleta: URL canônica, escopo administrativo, tipo de
conteúdo e coletor responsável.

## Regras

1. **Nada de contorno de bloqueio.** O agente se identifica
   (`AgendaCulturalSalvadorBot/1.0`), consulta `robots.txt`, não troca
   User-Agent, não usa proxy, não abre navegador headless para vencer WAF e
   não faz engenharia reversa de API privada. 401/403/429 encerram a coleta.
2. **Bloqueio é dado, não motivo para remover a fonte.** Fonte inacessível
   continua no catálogo com o motivo registrado e datado.
3. **Nada de evento inventado.** O coletor devolve o que a página publica; o
   que falta (hora, ano, local) é marcado em `campos_inferidos` e força
   revisão humana.
4. **Somente biblioteca padrão.** `requirements.txt` do projeto é vazio de
   propósito; o parser HTML é próprio (`collectors/htmldom.py`).
5. **Coletor não publica.** Ele não grava, não aprova e não define status.

## As 13 fontes institucionais obrigatórias

> **Atualizado em 2026-11-03:** o catálogo tem `TOTAL_FONTES = 31`:
> estas 13 fontes institucionais + as 13 plataformas de ingresso obrigatórias
> (`shotgun_salvador`, `shotgun_manifesto`, `ingresse`, `sympla_salvador`,
> `casa_rosa`, `eventim_salvador`, `bilheteria_digital_salvador`,
> `ticketmaker`, `ticketmaker_eventos`, `ticketmaster_brasil`, `bora_tickets`,
> `pida`, `nokta_salvador`) + 5 fontes UFBA/independentes (`ufba_agenda`,
> `ufba_escola_teatro`, `cine_glauber_rocha`, `agenda_ufba_portal`,
> `agenda_arte_cultura`). Sondagem e limitações em
> `docs/plataformas-ingressos.md`. Três delas têm coletor implementado e
> verificado com coleta real: `sympla_salvador`, `casa_rosa` e
> `bilheteria_digital_salvador` (ver `docs/coletores-plataformas.md`); as
> outras 10 seguem sem coletor e continuam `nao_suportada`/`bloqueada`.
> As 5 fontes UFBA/independentes estão em `docs/coletores-ufba.md` (sondagem
> bruta em `sources/probe_report_ufba.json`): 3 com coletor, 1 `indisponivel`
> por cadeia TLS incompleta e 1 `nao_suportada` por falta de data de evento
> estruturada.

Sondagem real de **2026-11-03T10:06Z** (`sources/probe_report.json`,
gerado por `python -m collectors.cli probe --salvar`):

| # | id | Fonte | Escopo | Tipo | Acesso | Resultado |
|---|----|-------|--------|------|--------|-----------|
| 1 | `pelourinho_dia_noite` | Pelourinho Dia e Noite — Agenda Completa | municipal | HTML | **bloqueada** | DNS ok (177.20.74.10), TCP sem SYN-ACK (timeout 25 s) |
| 2 | `secult_salvador` | SECULT Salvador | municipal | HTML | **bloqueada** | mesma faixa 177.20.74.0/24, TCP filtrado |
| 3 | `prefeitura_salvador` | Portal da Prefeitura | municipal | portal | **bloqueada** | 177.20.65.15, TCP filtrado |
| 4 | `tca` | Teatro Castro Alves — Calendário | estadual | JSON embutido | ok (200) | **171 eventos** brutos, 15 futuros |
| 5 | `funceb` | FUNCEB — Calendário | estadual | JSON embutido | ok (200) | calendário vivo, 1 evento (já passado) |
| 6 | `secult_ba` | SECULT-BA — Eventos | estadual | JSON embutido | ok (200) | calendário vivo, **0 eventos** |
| 7 | `ipac` | IPAC — Eventos | estadual | JSON embutido | ok (200) | calendário vivo, **0 eventos** |
| 8 | `teatro_vila_velha` | Teatro Vila Velha — Agenda Mensal | privado/OS | texto | ok (200) | **7 sessões** (texto livre) |
| 9 | `salvador_da_bahia` | Salvador da Bahia — Agenda do dia | municipal | cartões | ok (200) | **11 eventos** (sem hora) |
| 10 | `caixa_cultural_salvador` | Caixa Cultural Salvador | federal | HTML | **403** | WAF recusa o agente identificado |
| 11 | `mam_bahia` | MAM Bahia | estadual | HTML | **indisponível** | DNS não resolve (NXDOMAIN) |
| 12 | `muncab` | MUNCAB | privado | HTML | **403** | recusa requisição identificada |
| 13 | `sesc_bahia` | SESC Bahia — Programação | privado/Sistema S | SPA | **não suportada** | HTML de 10 KB sem eventos; dados em API interna não documentada |

Resumo: 6 acessíveis (4 com conteúdo), 5 bloqueadas, 1 indisponível,
1 não suportada. A faixa municipal `177.20.x` é inalcançável a partir desta
rede — só muda com rota legítima (ex.: executar no datacenter municipal) ou
espelho oficial publicado pelo próprio órgão.

> Se a lista de 13 URLs obrigatórias do projeto for outra, ela deve ser
> substituída em `sources/catalog.py` (constante `CATALOGO`). Todos os testes
> de integridade do catálogo continuam valendo.

## Tipos de coletor

| chave | classe | como extrai | confiança |
|-------|--------|-------------|-----------|
| `bagov_calendario` | `ColetorBaGovCalendario` | FullCalendar embutido em `drupalSettings` (JSON), sem executar JS | alta |
| `salvador_da_bahia_cartoes` | `ColetorSalvadorDaBahia` | `div.card` + cabeçalho do dia; hora ausente → meia-noite | média, revisão obrigatória |
| `vila_velha_texto` | `ColetorVilaVelha` | parágrafos `TÍTULO - dia(s) de mês - hora - local` | baixa, revisão obrigatória |
| `sympla_payload` | `ColetorSympla` | JSON de hidratação do Next.js publicado na listagem (datas ISO com offset) | alta |
| `casa_rosa_widget` | `ColetorCasaRosa` | iframe público do Sympla embutido na página; cartões server-rendered | média, ano inferido |
| `bilheteria_digital_cartoes` | `ColetorBilheteriaDigital` | cartões da busca + campos ocultos (`dataInicio`, `local`, preço) da página de detalhe | alta; baixa quando cai para o cartão |
| `ufba_agenda_tabela` | `ColetorAgendaUFBA` | linhas `table.views-table` da view Drupal (categoria, título, data, local) | alta |
| `ufba_teatro_json` | `ColetorTeatroUFBA` | `props.pageProps.agenda` do payload Next.js; UTC → America/Bahia | alta |
| `cine_glauber_sessoes` | `ColetorCineGlauber` | frase acessível "sessão de dd/mm/aaaa às HH:MM, sala N" por `div.sala-filme` | alta |
| `bloqueada` | `ColetorBloqueada` | não faz rede; levanta `FonteBloqueada` com o motivo do catálogo | — |
| `nao_suportada` | `ColetorNaoSuportada` | não faz rede; levanta `FonteNaoSuportada` | — |

Formato do calendário Drupal (todos os sites estaduais usam o mesmo tema):

```python
# collectors/bagov.py
def extrair_calendario(html: str, url_base: str) -> list[dict]:
    """lê '"events":[...]' logo após 'eventTimeFormat' e devolve o JSON"""
```

## Interfaces

```python
from sources.catalog import CATALOGO, por_id, fontes_coletaveis
from collectors import obter_coletor, ErroColeta

coletor = obter_coletor(por_id("tca"))
eventos = coletor.coletar(limite=20)          # list[EventoColetado]
evento = eventos[0]
evento.evento_kwargs()                        # -> database.models.Evento(**kwargs)
evento.proveniencia_kwargs(fonte_id=1, sessao_id=2, capturado_em=...)
evento.hash_conteudo()                        # sha256 exigido pela proveniência
```

`EventoColetado` (imutável) carrega `titulo`, `inicio_em` (com tz),
`fim_em`, `local_nome`, `endereco`, `descricao`, `preco`, `url_original`,
`metodo_extracao`, `confianca`, `campos_inferidos`, `revisao_necessaria`
(propriedade) e `trecho` (evidência).

Erros classificados: `FonteBloqueada` (401/403/429 ou bloqueio de rede
declarado), `FonteIndisponivel` (DNS/timeout/5xx), `RobotsProibido`,
`FonteNaoSuportada`, `RespostaInvalida`.

## Comandos

```bash
python -m collectors.cli list
python -m collectors.cli probe --json --salvar     # uma requisição por fonte
python -m collectors.cli collect --fonte tca --limite 5
python -m pytest tests/test_collectors.py -q       # 52 testes, sem rede
```

## Limitações conhecidas

- **TCA/SECULT-BA/FUNCEB/IPAC**: o calendário não traz local nem categoria; a
  página de detalhe traz (uma requisição por evento). Não implementado para
  não multiplicar requisições sem necessidade — fica para o enriquecimento.
- **Vila Velha**: texto livre, sem hora de término e com ano no cabeçalho do
  mês. Tudo entra com `confianca="baixa"`.
- **Salvador da Bahia**: só a agenda do dia (sem paginação histórica) e sem
  hora; o evento entra à meia-noite.
- **SESC**: exige API pública ou calendário servido no HTML.
- **Sites municipais**: dependem de rota de rede até `177.20.0.0/16`.
