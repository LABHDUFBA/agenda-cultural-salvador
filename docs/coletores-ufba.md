# Coletores UFBA e fontes independentes (2026-11-03)

Complemento a [`coletores-plataformas.md`](coletores-plataformas.md): aqui estão
as cinco fontes acrescentadas ao catálogo depois da sondagem de 2026-11-03, com
o que foi **observado** em cada uma, o que o coletor extrai e o que ficou de
fora — e por quê.

Regra que vale para todas: uma requisição por página, User-Agent identificado,
`robots.txt` consultado antes de cada host, sem retry em 401/403/429, sem proxy,
sem troca de identidade, sem desativar verificação de TLS. Fonte que recusa
acesso ou apresenta defeito de transporte fica registrada como
`bloqueada`/`indisponivel`; fonte sem parser fica `nao_suportada`. O catálogo
nunca converte essas situações em "cobertura".

Evidência bruta da sondagem: [`sources/probe_report_ufba.json`](../sources/probe_report_ufba.json).
Testes: [`tests/test_coletores_ufba.py`](../tests/test_coletores_ufba.py)
(32 casos, todos offline, com recortes reais em `tests/fixtures/`).

## Resumo

| fonte | acesso | coletor | resultado real da coleta |
|---|---|---|---|
| `ufba_agenda` | 200, robots `Allow: /` | `ufba_agenda_tabela` | 8 eventos, 0 inferidos |
| `ufba_escola_teatro` | 200, robots `Allow: /` | `ufba_teatro_json` | 63 registros lidos, 0 futuros hoje |
| `cine_glauber_rocha` | 200, robots `Allow` | `cine_glauber_sessoes` | 57 sessões, 48 filmes |
| `agenda_ufba_portal` | TLS com cadeia incompleta | — (bloqueada) | não coletado |
| `agenda_arte_cultura` | 200, robots permite | — (não suportada) | não coletado |

## 1. Agenda UFBA — `ufba.br/agenda-ufba`

O portal principal (`ufba.br`, que redireciona de `www.ufba.br`) publica a
agenda como uma *view* Drupal renderizada no servidor. Não há JSON nem
JavaScript: os dados estão no HTML entregue, em `table.views-table`.

Cada linha tem parágrafos com classe própria:

- `p.agenda-categoria` — Cursos, Eventos, Artes, Defesas, Palestras…;
- `p.agenda-titulo > a` — título e URL do evento;
- `p.agenda-data` — `span.date-display-single` (evento de um dia) ou
  `span.date-display-start` + `span.date-display-end` (intervalo), no formato
  `dd/mm/aaaa - HhMM`;
- `p.agenda-local` — local publicado;
- demais parágrafos da célula — descrição.

O coletor `ufba_agenda_tabela` (`collectors/ufba_agenda.py`) lê exatamente
esses campos, absolutiza a URL e não infere nada: `campos_inferidos` fica vazio
e a confiança é `alta`. A hora é a digitada pelo editor — inclusive valores de
preenchimento como `8h08` — e **não** é arredondada nem "corrigida".

Resultado da coleta real em 2026-11-03: **8 eventos**, do curso de outubro ao
Programa de Verão de janeiro/2027. Eventos com fim publicado usam o fim no
corte de passados; os demais, o início.

Limite conhecido: a página entrega a primeira página da view (8 linhas) e não
publicou pager no HTML. O coletor não inventa `?page=` — se a UFBA paginar a
lista, o parâmetro terá de ser observado e registrado antes de ser usado.

## 2. Escola de Teatro da UFBA — `teatro.ufba.br/agenda/`

App Next.js. O HTML entregue contém o payload de build em
`<script id="__NEXT_DATA__" type="application/json">`, com a lista completa em
`props.pageProps.agenda`. Cada objeto traz:

- `uid` — usado para montar `/agenda/<uid>/`;
- `title`, `subtitulo`, `local`, `tipo` (lista de categorias);
- `date` — ISO 8601 com offset, ex. `2026-03-31T18:56:00+0000`.

`collectors/ufba_teatro.py` interpreta o JSON (sem executar JS) e **converte**
UTC para `America/Bahia`. Objeto sem offset é descartado, não reinterpretado.
Nada mais é inferido.

Estado real em 2026-11-03: 63 registros, o mais recente em **31/03/2026** — a
unidade não tinha evento futuro publicado. A coleta devolve zero e o coletor
registra `{"agenda_encontrada": true, "registros": 63, "eventos": 0}`. Zero
aqui é ausência na fonte, não falha do parser; por isso a fonte permanece
`ok` e o fato está na observação do catálogo.

## 3. Cine Glauber Rocha — `cineglauberrocha.com.br/glauber-rocha/programacao`

Cinema de rua ligado à UFBA. A programação é HTML renderizado no servidor:
48 blocos `div.filme`, 57 `div.sala-filme` na sondagem. Cada bloco traz
`h2.programacao-filme-title`, o link `/glauber-rocha/filme/<id>/<slug>` e, por
sessão, `div.sala`, `div.audio`, o link de compra e — o dado que importa — a
frase acessível publicada para leitores de tela:

```
ingresso para 100 Dias, sessão de 03/11/2026 às 17:10, sala 3, NAC 2D
```

`collectors/cine_glauber.py` tira data, hora e sala **dessa frase**. O
parâmetro `sessao=ddmmaaaa_hhmm` da URL de ingresso não é usado como fonte de
data (seria reconstruir o dado por engenharia reversa de parâmetro); a URL entra
apenas como `url_ingresso`, e a página do filme como `url_original`.

Resultado real: **57 sessões** (filme + sala + horário), confiança `alta`,
`campos_inferidos` vazio. `local_nome` combina o cinema (fonte) com a sala
publicada — composição, não inferência.

## 4. `agenda.ufba.br` — registrada, não coletada

O portal separado de eventos tem certificado válido para
`*.agenda.ufba.br` (GlobalSign), mas o servidor **não envia o certificado
intermediário** `GlobalSign RSA OV SSL CA 2018`. Resultado do cliente do
projeto:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate
```

Desativar verificação, fixar CA no código ou passar por proxy resolveria para o
coletor e criaria um risco desnecessário; nenhuma dessas coisas foi feita. A
fonte fica no catálogo como `indisponivel` (sem `http_status`, porque não houve
resposta HTTP) até o servidor corrigir a cadeia. O mesmo conteúdo institucional
é coletado de `ufba.br/agenda-ufba`, que responde 200.

## 5. Agenda Arte e Cultura — registrada, não coletada

`agendartecultura.com.br` é uma publicação independente que se apresenta como
"agenda digital artística e cultural da Universidade e de Salvador", com
matérias por linguagem e menu de equipamentos culturais da UFBA. É a fonte
independente mais próxima do escopo do projeto — e **não** é coletável hoje.

Motivo objetivo: o WordPress não publica a data do evento em campo estruturado.
As tags `<time>` trazem a data de **publicação** da matéria (`entry-date
published`), e a data do evento aparece apenas no texto corrido. Usar a data de
publicação como data de evento produziria eventos falsos; extrair do texto
corrido seria o mesmo parsing frágil do Teatro Vila Velha, mas sem nem o
cabeçalho de mês/ano que aquele site oferece. Fica `nao_suportada`, com o
motivo no catálogo, e pode virar coletor se o site adotar schema.org `Event` ou
um campo de data.

## O que **não** foi feito

- Nenhuma fonte com login, paywall, 403/429 ou WAF foi contornada.
- Nenhum endpoint privado foi inferido a partir de SPA.
- Nenhum evento foi criado a partir de notícia, data de publicação ou texto
  promocional sem data explícita.
- Nenhuma coordenada, hora ou local foi estimado: quando a fonte não publica,
  o campo fica vazio e o núcleo registra a ausência.
- `bot/`, `database/` e `scripts/` não foram alterados.

## Como reproduzir

```bash
.venv/bin/python -m collectors.cli list
.venv/bin/python -m collectors.cli probe --fonte ufba_agenda --json
.venv/bin/python -m collectors.cli collect --fonte ufba_agenda
.venv/bin/python -m collectors.cli collect --fonte cine_glauber_rocha --limite 5
.venv/bin/python -m pytest tests/test_coletores_ufba.py -q
```

Nenhum comando acima grava no banco sem `--gravar`.
