# Auditoria de viabilidade — evidências medidas (03/11/2026)

Tudo abaixo foi executado offline neste repositório (sem rede, sem credenciais).
Suíte: `pytest tests` → **307 passed, 2 skipped** (os 2 skips são provas de rede,
opt-in por `AGENDA_PROBE_REDE=1`).

## Cobertura real de fontes

`python -m collectors.cli list` → `total=31  coletáveis=12`

| acessibilidade | n | significado |
|---|---|---|
| ok | 12 | alcançável **e** com coletor: `tca`, `funceb`, `secult_ba`, `ipac`, `teatro_vila_velha`, `salvador_da_bahia`, `sympla_salvador`, `casa_rosa`, `bilheteria_digital_salvador`, `ufba_agenda`, `ufba_escola_teatro`, `cine_glauber_rocha` |
| bloqueada | 9 | 403/429/WAF ou TCP filtrado (Pelourinho, SECULT Salvador, Prefeitura, Caixa Cultural, MUNCAB, Shotgun×2, Ingresse, Ticketmaster) |
| indisponivel | 3 | DNS/timeout/TLS (MAM-BA sem registro ativo, Eventim, `agenda.ufba.br` com cadeia TLS incompleta) |
| nao_suportada | 7 | alcançável, sem parser (SESC SPA, Ticketmaker×2, Bora, Pida, Nokta, Agenda Arte e Cultura sem data de evento estruturada) |

As cinco fontes UFBA/independentes e o que foi medido em cada uma estão em
[`docs/coletores-ufba.md`](coletores-ufba.md); a sondagem bruta, em
`sources/probe_report_ufba.json`.

**As 13 bilheterias/plataformas de ingresso obrigatórias estão no catálogo com
`http_status` da sondagem real.** Três têm coletor verificado com coleta real
em 2026-11-03 (Sympla Salvador, Casa Rosa via widget público do Sympla,
Bilheteria Digital — ver `docs/coletores-plataformas.md`); as outras 10 seguem
sem coletor e não são `coletavel`, então não há coleta que não exista sendo
anunciada.

Consequência prática: o núcleo municipal (Pelourinho/SECULT/Prefeitura) está
bloqueado por rede; a operação hoje depende de fontes estaduais (TCA, FUNCEB,
SECULT-BA, IPAC) e de dois sites privados/municipais (Vila Velha, Salvador da
Bahia). Sem rota de rede legítima para `177.20.64.0/18`, a "agenda oficial da
prefeitura" não é coletável por este processo.

## Pipeline verificado ponta a ponta (offline)

`collectors.cli`/fixture real do calendário do TCA:

```
coletados=3 aceitos=3 rejeitados=0 duplicados=0
segunda passada: duplicados=3 aceitos=0        # dedupe por chave canônica
python -m collectors.cli pendentes --json      # 3 linhas 'pendente' com proveniência
```

Também exercitados com sucesso: `bot.app --self-check`, `scripts/demo_offline.py`
(ciclo completo: envio → negativa de visitante → autorização → aprovação →
dry-run → bloqueio de execução real → auditoria), `scripts/preflight.py --mode dev`,
`scripts/healthcheck.py`.

## Defeitos corrigidos nesta auditoria

1. **Fuso no portão temporal** (`bot/publisher.py`): `starts_at` é horário local
   de Salvador sem offset; era interpretado como UTC, adiantando o evento em 3 h.
   Um evento que começaria em 1 h era bloqueado como "evento já ocorreu".
   Agora converte via `database.models.para_dt` (Bahia → UTC).
2. **Truncamento silencioso em 4096** (`bot/telegram_api.py`): `send_message`
   cortava `text[:4096]`; o fim de `/auditoria` e listagens longas era perdido.
   Agora divide em partes; o teclado inline vai na última.
3. **`split_text` perdia o resto de uma linha maior que o limite** — corrigido e
   testado (linha de ~8,7 k caracteres).
4. **Botões inline eram código morto**: `getUpdates` pedia
   `allowed_updates=["message"]` e o `callback_query` nunca chegava ao handler;
   além disso o callback nunca era respondido (spinner até expirar). Agora
   `callback_query` é assinado, normalizado (`callback_data`/`callback_id`) e
   respondido via `answerCallbackQuery` em `bot/app.py`.
5. **Perda de `@username`** (`bot/db.py`): `upsert` com username vazio apagava o
   nome gravado — `/papel ID perfil` e o bootstrap de `ADMIN_IDS` passam vazio.
   Agora username vazio preserva o existente.
6. **`scripts/demo_offline.py` quebrado no comando documentado**: o README manda
   `ADMIN_IDS=123456789`, mas a demo usa os atores 9001/9002/9003; sem
   `ADMIN_IDS=9001` todas as ações administrativas eram negadas. A demo agora
   registra o próprio admin e é autossuficiente.

Regressões: `tests/test_transporte_e_portao.py` (11 testes).

## Lacunas conhecidas que permanecem (não corrigidas aqui)

- **`municipio` é declaração, não prova.** `collectors/models.py` grava
  `Salvador` por padrão em *toda* coleta, inclusive nos calendários estaduais
  (SECULT-BA, FUNCEB, IPAC). O portão de publicação exige evidência
  independente (coordenadas na caixa do município, trecho da fonte com
  "Salvador", ou declaração de operador identificado com autorização
  registrada). Nenhum coletor preenche coordenadas, então na prática a evidência
  é texto de trecho ou atestação humana — e **um único admin pode atestar
  município e autorização** (`/autorizar` aceita referência livre). O sistema não
  verifica localização sozinho; ele exige que alguém responda por ela.
- **Linhas com `municipio='Salvador'` de calendário estadual** continuam
  gravadas assim (bloqueadas para publicação, mas enganosas em relatório/CLI).
- `bot.app --self-check` **escreve** no banco: `build_runtime` promove
  `ADMIN_IDS` para admin. Uma "checagem" não deveria alterar estado.
- **CLI administrativo não aplica papéis**: quem tem shell é admin. O envio real
  continua exigindo `AGENDA_ALLOW_EXECUTE=1` + frase `PUBLICAR` por comando.
- `Throttle` é por usuário, em memória, sem poda de ids inativos (cresce com o
  número de usuários distintos) e zera a cada reinício.
- Dedupe é exato (título+início+local+município): duplicatas *quase* iguais de
  fontes diferentes não são unificadas; `/lote` não retoma itens bloqueados.
- Transporte HTTP e Bot API **não foram exercitados em rede** (sem token): o
  caminho é testado por injeção de `_call`, não contra a API real.
