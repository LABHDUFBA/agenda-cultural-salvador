# 13 plataformas de ingresso — sondagem e status de integração

**Data da sondagem:** 2026-11-03 (UTC), a partir desta rede.
**Relatório bruto:** `sources/probe_report_plataformas.json`.
**Testes:** `tests/test_plataformas_ingressos.py` + `tests/test_coletores_plataformas.py`.
**Coletores implementados:** Sympla Salvador, Casa Rosa e Bilheteria Digital —
detalhes, evidência de coleta e limitações em
[`docs/coletores-plataformas.md`](coletores-plataformas.md).

## Método (ético, sem contorno)

Cada URL foi consultada com o `collectors.fetch.HttpFetcher` do projeto:

- `User-Agent: AgendaCulturalSalvadorBot/1.0 (...)` — o agente se identifica,
  não finge navegador;
- `robots.txt` consultado antes do host (RFC 9309 quando ausente/erro);
- timeout de 15–25 s, sem retry em bloqueio, sem proxy, sem rotação de UA, sem
  navegador headless;
- 401/403/429 encerram a tentativa — bloqueio é registrado, não vencido.

Nenhum endpoint privado foi inspecionado, nenhum login/captcha foi tentado e
nenhum evento foi publicado.

## Resultado real

| # | id | Status | Bytes | robots | Acessibilidade no catálogo | Coletor |
|---|----|--------|-------|--------|---------------------------|---------|
| 1 | `shotgun_salvador` | **429** | — | — | bloqueada | `bloqueada` |
| 2 | `shotgun_manifesto` | **429** | — | — | bloqueada | `bloqueada` |
| 3 | `ingresse` | **403** | — | — | bloqueada | `bloqueada` |
| 4 | `sympla_salvador` | 200 | 428.117 | permitido | **ok** | `sympla_payload` — 31 eventos |
| 5 | `casa_rosa` | 200 | 10.857 | ausente | **ok** | `casa_rosa_widget` — 5 eventos (iframe público) |
| 6 | `eventim_salvador` | **timeout de leitura (15 s)** | — | — | indisponivel | `bloqueada` |
| 7 | `bilheteria_digital_salvador` | 200 | 120.974 | permitido | **ok** | `bilheteria_digital_cartoes` — 8 eventos |
| 8 | `ticketmaker` | 200 | 973.329 | permitido | nao_suportada | `nao_suportada` |
| 9 | `ticketmaker_eventos` | 200 | 221.647 | permitido | nao_suportada | `nao_suportada` |
| 10 | `ticketmaster_brasil` | **403** | — | — | bloqueada | `bloqueada` |
| 11 | `bora_tickets` | 200 | 56.768 | permitido | nao_suportada | `nao_suportada` |
| 12 | `pida` | 200 | 1.311.237 | permitido | nao_suportada | `nao_suportada` |
| 13 | `nokta_salvador` | 200 | 619.886 | permitido | nao_suportada | `nao_suportada` |

Resumo: **8 alcançáveis (HTTP 200)**, **4 bloqueadas por decisão do servidor**
(2×429, 2×403), **1 indisponível** (read timeout). Catálogo total: **9
coletáveis** (6 fontes institucionais + 3 plataformas) e 17 sem coleta.

## O que foi feito nestas 3 e o que continua fora

**Implementado e verificado com coleta real em 2026-11-03** (código em
`collectors/`, testes offline em `tests/test_coletores_plataformas.py`):

- `sympla_salvador` → `json_embutido` (payload de hidratação que a própria
  página publica; datas ISO com offset, local, endereço, UF, coordenadas).
- `casa_rosa` → `cartao_html` (iframe público do Sympla embutido na página;
  ano não é publicado e vai como campo inferido).
- `bilheteria_digital_salvador` → `atributo_html` (cartões da busca + campos
  ocultos `dataInicio`/`local`/preço na página de detalhe).

**Continua sem coletor** (e sem fingir cobertura):

- `ticketmaker`, `ticketmaker_eventos`, `bora_tickets`, `pida`,
  `nokta_salvador`: listagem nacional server-rendered; exigem filtro municipal
  e parser próprio antes de qualquer coleta.
- `shotgun_salvador`, `shotgun_manifesto`, `ingresse`, `ticketmaster_brasil`:
  sem rota legítima hoje (403/429). Reavaliar apenas com canal oficial.
- `eventim_salvador`: read timeout; reavaliar com uma nova tentativa espaçada
  e registrada.

**Retificação:** a observação anterior da Bilheteria Digital ("resultados vêm
de AJAX") estava errada — o inventário contava apenas `href` absolutos. Os
cartões estão no HTML entregue (9 no total, 8 de Salvador). O catálogo foi
corrigido junto com o coletor.

## Nada disso é publicado

- Coletores devolvem `EventoColetado`; não gravam, não aprovam, não publicam.
- Persistência só com `python -m collectors.cli collect --gravar`, que grava
  como `pendente`/`rejeitado` via `collectors.ingest`.
- `bot/`, `database/`, agendador e Docker não foram alterados.
- Nenhum contorno de bloqueio em Shotgun, Ingresse ou Ticketmaster.

## Por que as outras seguem `nao_suportada`

`acessibilidade` responde "consigo coletar?", não só "o host respondeu". O
`http_status` real (200) fica preservado em cada entrada. Marcar `ok` sem
parser faria `coletavel=True` e o agendador tentaria uma fonte sem coletor —
mentira operacional. A semântica está no docstring de `sources/catalog.py` e é
verificada por `tests/test_plataformas_ingressos.py`.

## Persistência verificada

- O catálogo (31 fontes) serializa em JSON, é relido do disco e permanece
  idêntico; um processo novo enxerga as 31 fontes.
- O relatório de sondagem é persistido com `gerado_em`, política de rede e os
  13 resultados; cada entrada do catálogo concorda com o status registrado
  (fonte com `acessibilidade="ok"` precisa ter coletor real no registro).

## Como reproduzir

```bash
# sem rede (padrão)
.venv/bin/python -m pytest tests/test_plataformas_ingressos.py tests/test_coletores_plataformas.py -q

# coleta real das 3 implementadas, sem gravar
.venv/bin/python -m collectors.cli collect --fonte sympla_salvador --limite 5 --json
.venv/bin/python -m collectors.cli collect --fonte casa_rosa --limite 5 --json
.venv/bin/python -m collectors.cli collect --fonte bilheteria_digital_salvador --limite 5 --json

# sondagem real das 13 (uma requisição por fonte)
AGENDA_PROBE_REDE=1 .venv/bin/python -m pytest \
  tests/test_plataformas_ingressos.py::TestAcessoReal -q
```

## Próximos passos técnicos

1. **Ticketmaker / Pida / Nokta / Bora**: listagens server-rendered — parser de
   cartões por site, filtro municipal por cidade publicada e deduplicação por
   URL canônica; antes disso, conferir robots/ToS de cada plataforma.
2. **Eventim**: nova tentativa espaçada com timeout maior, registrando o
   resultado (sem contorno).
3. **Ingresse / Ticketmaster / Shotgun**: sem rota legítima hoje; só com canal
   oficial (API/parceria).
