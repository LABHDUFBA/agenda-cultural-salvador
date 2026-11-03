# Coletores de plataformas de ingresso — Sympla, Casa Rosa e Bilheteria Digital

**Implementado e verificado em:** 2026-11-03 (UTC), a partir desta rede.
**Código:** `collectors/sympla.py`, `collectors/casa_rosa.py`,
`collectors/bilheteria_digital.py`; registro em `collectors/registry.py`.
**Testes offline:** `tests/test_coletores_plataformas.py` (25 casos, zero rede).
**Amostras de página (2026-11-03):** `tests/fixtures/sympla_salvador.html`
(recorte de 3 eventos: payload e cartões verbatim, e-mails de organizador
removidos), `casa_rosa.html` e `casa_rosa_widget.html` (capturas completas,
sem PII), `bd_busca.html` (4 cartões verbatim, SVG e `data-src` removidos) e
`bd_evento.html` (só os campos ocultos publicados e o preço — tokens de
sessão/carrinho não foram copiados).

## Política de acesso aplicada (sem exceção)

| Regra | Como aparece no código |
|-------|------------------------|
| Agente identificado | `HttpFetcher` usa `AgendaCulturalSalvadorBot/1.0`; nenhum UA de navegador |
| `robots.txt` antes de cada host | `HttpFetcher._robots_para` (RFC 9309 quando ausente) |
| 401/403/429 encerram a coleta | `FonteBloqueada` sobe; nenhum retry, proxy ou headless |
| Uma requisição por página, com intervalo mínimo | `intervalo_minimo=1.0 s` por host |
| Sem API privada, sem engenharia reversa, sem JS | só o HTML entregue pelo servidor |
| Sem inferir cidade, data ou hora | cidade/hora vêm da fonte; o que falta vai em `campos_inferidos` |
| Coletor não grava, não aprova, não publica | devolve `EventoColetado`; persistência só via `--gravar` |

## Evidência de acesso (2026-11-03, `cli probe` + `cli collect`)

| Fonte | HTTP | Bytes | robots | Coletor | Resultado da coleta |
|-------|------|-------|--------|---------|---------------------|
| `sympla_salvador` | 200 | 428.117 | permitido | `sympla_payload` | **31 eventos** (45 objetos, 31 únicos por URL, 0 descartados) |
| `casa_rosa` (página) | 200 | 10.857 | ausente | `casa_rosa_widget` | 5 eventos, via iframe (2ª requisição: 23.262 bytes, 200, permitido) |
| `bilheteria_digital_salvador` | 200 | 120.974 | permitido | `bilheteria_digital_cartoes` | **8 eventos** Salvador (9 cartões; 1 de Ituberá descartado), 8 páginas de detalhe lidas, 0 falhas |

Nenhuma das três exigiu login, captcha, cookie de sessão ou qualquer coisa
além de um `GET` identificado.

## Como cada coletor lê o dado

### Sympla — `json_embutido`, confiança alta

A listagem é Next.js (App Router) e o HTML entregue já contém o payload de
hidratação (`self.__next_f.push([1, "…"])`). O coletor concatena os pedaços,
recorta os objetos JSON completos com uma varredura consciente de strings e
mantém os que têm `url` + `start_date`:

- `name`, `url` (canônica do evento), `start_date`/`end_date` ISO com offset,
  `location.{name,address,city,state,neighborhood,lat,lon}`,
  `start_date_formats.pt` (legenda publicada, usada no `trecho`);
- **1 requisição**; nada de endpoint adivinhado;
- deduplicação por URL canônica (o payload repete eventos em outras listas);
- só entra evento com `location.city == Salvador` e `state == BA` — o resto é
  descartado, nunca reinterpretado;
- data convertida de UTC para America/Bahia (conversão determinística, não
  inferência); `campos_inferidos` fica vazio.

### Casa Rosa — `cartao_html`, confiança média

A página institucional não lista evento nenhum: publica um `<iframe>` público
do Sympla (`sympla.com.br/agenda-eventos/<token>?theme=light`). O coletor segue
esse iframe — URL publicada pela própria página — e lê os cartões
server-rendered:

- `data-name` (nome **completo**; o texto visível é truncado por CSS),
  `.event-date-day`, `.event-card-date-month` (mês abreviado),
  `.event-card-info` (hora), `.event-location` ("Casa Rosa - BA");
- **2 requisições** (página + iframe);
- a fonte **não publica o ano**: ele é inferido por `ano_provavel` e sempre
  declarado em `campos_inferidos=("ano",)`, o que força revisão humana;
- se a página deixar de trazer o widget, o coletor levanta
  `FonteNaoSuportada` em vez de devolver zero evento em silêncio.

### Bilheteria Digital — `atributo_html`, confiança alta (com fallback declarado)

A observação anterior ("resultados vêm de AJAX") estava **errada**: os
resultados estão no HTML (o inventário antigo contava só `href` absolutos).
O coletor:

1. lê `/busca/salvador` (1 requisição) e extrai `li.box-li-evento` →
   título, link, `.data-evento-thumb` ("15 de Novembro"), `.cidade-box-evento`,
   `.local-box-evento`;
2. mantém **apenas** `Salvador - BA` (o cartão de Ituberá é descartado);
3. lê a página de detalhe de cada cartão (link publicado no próprio cartão) e
   usa os campos ocultos renderizados pelo servidor: `nomeEvento`, `local`,
   `dataInicio` ISO com `Z` → convertido para America/Bahia, e o preço
   "A PARTIR DE R$ …"; nesse caminho `confianca="alta"` e nenhum campo é
   inferido;
4. se o detalhe falhar por motivo que **não** seja bloqueio, cai para dia/mês
   do cartão, marcando `ano` e `hora` como inferidos e `confianca="baixa"`;
5. bloqueio (401/403/429) na busca **ou** no detalhe sobe como
   `FonteBloqueada` — nunca é contornado;
6. teto de 25 páginas de detalhe por execução (`max_detalhes`), para não
   transformar a coleta em varredura.

## Amostras reais (recortadas, com proveniência)

```json
{"fonte_id": "sympla_salvador", "titulo": "TRIO SITKOVETSKY NO TCA",
 "inicio_em": "2026-11-03T20:00:00-03:00",
 "url_original": "https://bileto.sympla.com.br/event/127058",
 "local_nome": "Teatro Castro Alves", "metodo_extracao": "json_embutido",
 "confianca": "alta", "campos_inferidos": [],
 "trecho": "TRIO SITKOVETSKY NO TCA — Ter, 03 Nov - 2026 · 20:00 — Teatro Castro Alves, Salvador/BA"}

{"fonte_id": "casa_rosa", "titulo": "UANA apresenta: A Estrela",
 "inicio_em": "2026-11-06T22:00:00-03:00",
 "url_original": "https://bileto.sympla.com.br/event/127486",
 "local_nome": "Casa Rosa", "metodo_extracao": "cartao_html",
 "confianca": "media", "campos_inferidos": ["ano"],
 "trecho": "UANA apresenta: A Estrela — 06 Nov 2026 — 22:00 — Casa Rosa"}

{"fonte_id": "bilheteria_digital_salvador", "titulo": "ENSAIO DE NARCIZINHO",
 "inicio_em": "2026-11-15T17:00:00-03:00",
 "url_original": "https://www.bilheteriadigital.com/ensaio-de-narcizinho-15-de-novembro",
 "local_nome": "ESPAÇO CULTURAL DA BARROQUINHA", "preco": "R$ 50,00",
 "metodo_extracao": "atributo_html", "confianca": "alta",
 "campos_inferidos": []}
```

`17:00` confere com o horário que a própria página de detalhe exibe
("Domingo às 17:00") e com `dataInicio=2026-11-15T20:00:00.000Z` (UTC).

## O que este trabalho NÃO faz

- **Não publica e não grava.** `--gravar` continua sendo a única porta, e ela
  grava como `pendente`/`rejeitado` via `collectors.ingest` (inalterado).
- **Não toca bot, banco, agendador ou Docker.** Só `collectors/`, `sources/`,
  `tests/` e `docs/`.
- **Não implementa as outras 10 plataformas.** `ticketmaker`,
  `ticketmaker_eventos`, `bora_tickets`, `pida`, `nokta_salvador` seguem
  `nao_suportada` (listagem nacional, exige filtro municipal + parser próprio);
  `shotgun_*`, `ingresse`, `ticketmaster_brasil` seguem `bloqueada` (403/429);
  `eventim_salvador` segue `indisponivel` (read timeout).
- **Não completa dado que a fonte não publica.** Preço do Sympla, endereço do
  Casa Rosa e horário sem detalhe na Bilheteria Digital ficam vazios ou
  inferidos e marcados — nunca preenchidos por conta própria.

## Reproduzir

```bash
# offline (padrão, zero rede)
.venv/bin/python -m pytest tests/test_coletores_plataformas.py -q

# coleta real, sem gravar nada
.venv/bin/python -m collectors.cli collect --fonte sympla_salvador --limite 5 --json
.venv/bin/python -m collectors.cli collect --fonte casa_rosa --limite 5 --json
.venv/bin/python -m collectors.cli collect --fonte bilheteria_digital_salvador --limite 5 --json
```

## Limitações conhecidas

1. O parser do Sympla depende de o Next.js continuar publicando o payload de
   hidratação. Se mudar, o teste de fixture passa mas a coleta real falha com
   `FonteNaoSuportada` — é o comportamento desejado (falha visível).
2. O widget do Casa Rosa é `theme=light` e pode mudar de markup sem aviso; o
   token do iframe é estável desde 2019, mas não é garantia.
3. A Bilheteria Digital exige N+1 requisições (busca + detalhes). Com 8 eventos
   hoje é barato; o teto de 25 evita crescimento descontrolado.
4. `dataInicio` da Bilheteria Digital vem em UTC (`Z`). A conversão para
   America/Bahia foi conferida contra o texto visível da página; se o site
   passar a publicar hora local com `Z`, o horário sairia 3 h adiantado — o
   `trecho` preserva a data do cartão para auditoria.
