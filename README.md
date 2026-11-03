# Agenda Cultural Salvador — bot de consulta e administração

Bot de Telegram para consulta pública de eventos culturais de Salvador e para
administração **com autorização explícita**. Projetado para funcionar 100%
offline em desenvolvimento: sem token, sem rede, sem credenciais.

## Estrutura

```
bot/
  models.py       domínio puro: Event, EventStatus, UserRole, validação
  db.py           Protocolos de repositório + implementação SQLite (esquema, auditoria)
  permissions.py  autorização no servidor (perfil lido do banco pelo telegram_id)
  publisher.py    publicação conservadora (dry-run padrão, idempotente, com limites)
  handlers.py     comandos → mensagens; nunca faz I/O de rede
  telegram_api.py fronteira de transporte: OutgoingMessage, NullTransport, HttpTransport
  runtime.py      montagem das dependências (SQLite + serviços)
  config.py       configuração por ambiente, segura por padrão
  admin.py        CLI offline de operação
  app.py          loop de long-polling (recusa iniciar sem BOT_TOKEN)
feiras/
  catalogo.py     feiras de Salvador com trecho de fonte por campo
  repository.py   carga idempotente e consulta (SQLite, schema canônico)
  cli.py          carregar | listar | buscar | evidencias | fontes | verificar
tests/test_bot.py 32 testes, todos offline
scripts/demo_offline.py  passeio completo pelo ciclo de vida
```

Dependências de execução: **apenas biblioteca padrão** (sqlite3, urllib, json).
`pytest` é usado somente para testes.

## Modelo de segurança

- **Identidade nunca vem do cliente.** O perfil é resolvido por `telegram_id`
  do envelope do update, consultando a tabela `users`. Campos de texto,
  `callback_data` e nomes de usuário não concedem nada.
- **Autorização no servidor, por comando.** `PermissionService.require()` é
  chamado dentro de cada handler administrativo — não existe "modo admin" no
  cliente nem filtro por menu.
- **Negativas são auditadas** (`audit_log.outcome = 'denied'`), inclusive
  tentativas de visitante em `/aprovar` ou `/publicar`.
- **Nenhum parse_mode**: o texto do usuário é enviado como texto puro, então
  marcação HTML/Markdown não é interpretada. Caracteres de controle são
  removidos e os campos têm limite de tamanho.
- **Sem credenciais no código.** Token só por `BOT_TOKEN`; ausente ⇒ o processo
  recusa iniciar e nenhuma conexão é tentada.

## Publicação conservadora (5 travas independentes)

`Publisher.publish()` só envia quando **todas** valem:

1. Quem pede é `admin` no banco.
2. O evento está `approved` **e** `authorization_confirmed` **e** tem
   `authorization_reference` (quem autorizou, quando), fonte HTTP verificável e
   descrição.
3. O evento não está no passado.
4. `dry_run=False` (padrão é `True`) **e** a frase exata `confirmar PUBLICAR`
   foi digitada **e** `AGENDA_ALLOW_EXECUTE=1` no ambiente.
5. Respeito ao intervalo mínimo entre envios (`AGENDA_MIN_INTERVAL`, 300 s) e ao
   teto por lote (`AGENDA_MAX_PER_RUN`, 3).

Além disso: já publicado ⇒ `skipped` (nunca reenvia); falha de transporte não
marca como publicado e fica registrada em `publish_attempts` com `outcome=failed`.

**Município comprovado, não declarado.** O campo `municipio` é preenchido por
padrão (`Salvador`) tanto pelo coletor quanto pelo formulário do bot, então ele
sozinho não prova nada. O portão exige uma evidência independente: coordenadas
gravadas dentro da caixa de Salvador, trecho capturado da fonte que nomeia
Salvador, ou submissão humana identificada com autorização confirmada e
referência. Sem isso o bloqueio é `municipio_salvador_sem_evidencia`;
coordenada fora da cidade junto de `municipio=Salvador` vira
`municipio_salvador_contradito_por_coordenadas` (nenhum dos dois lados é
descartado em silêncio).

`/rejeitar` grava o motivo em `eventos.motivo` e a auditoria append-only
preserva o estado anterior e o motivo que a ingestão havia registrado; a
proveniência nunca é apagada. Evento já publicado não pode ser rejeitado.

## Comandos

Consulta (qualquer pessoa):

- Janelas: `/hoje`, `/amanha`, `/semana` (7 dias), `/fimdesemana` (sábado e domingo).
- Recortes: `/gratis` (30 dias), `/musica`, `/teatro`, `/cinema`, `/exposicoes`,
  `/noite` (a partir das 18h), `/ufba`, `/bairro [nome]` (sem nome lista os bairros).
- Catálogo e busca: `/fontes`, `/agenda`, `/evento ID`, `/buscar termo`, `/ajuda`.
- Feiras: `/feiras` (hoje), `/feiras sabado`, `/feiras bairro Liberdade`,
  `/feira SLUG` (ficha com o trecho e a URL da fonte). Dados e limites em
  [`docs/feiras.md`](docs/feiras.md).

Colaboração (`editor`+): `/novo Título | Local | AAAA-MM-DDTHH:MM | link | Descrição`, `/meus`.

Administração (`admin`): `/pendentes`, `/autorizar ID referência`, `/aprovar ID`,
`/rejeitar ID motivo`, `/publicar ID [confirmar PUBLICAR]`, `/lote`,
`/auditoria ID`, `/corrigir ID campo=valor`, `/duplicados`, `/atualizar`, `/status`,
`/usuarios`, `/papel ID perfil`.

Regras de recorte e limites: ver [`docs/comandos.md`](docs/comandos.md).

## Uso

```bash
python3 -m venv .venv && .venv/bin/pip install pytest

# operação offline
export AGENDA_DB=data/agenda.db
.venv/bin/python -m bot.admin init --admin 123456789
.venv/bin/python -m bot.admin events --status pending_review
.venv/bin/python -m bot.admin authorize 1 "e-mail da produtora, 01/11/2026"
.venv/bin/python -m bot.admin approve 1 --actor 123456789
.venv/bin/python -m bot.admin publish 1 --actor 123456789   # dry-run
.venv/bin/python -m bot.admin audit 1

# demonstração do ciclo completo, sem rede
ADMIN_IDS=123456789 .venv/bin/python scripts/demo_offline.py

# checagem de fiação
.venv/bin/python -m bot.app --self-check

# produção (só com token + canal definidos)
BOT_TOKEN=... TARGET_CHAT_ID=-100... AGENDA_ALLOW_EXECUTE=0 .venv/bin/python -m bot.app
```

Variáveis: `BOT_TOKEN`, `TARGET_CHAT_ID`, `AGENDA_DB`, `AGENDA_ALLOW_EXECUTE`,
`AGENDA_MAX_PER_RUN`, `AGENDA_MIN_INTERVAL`, `ADMIN_IDS` (bootstrap),
`TELEGRAM_API_BASE`.

## Testes

```bash
.venv/bin/python -m pytest tests/test_bot.py -q     # 32 passed
```

Cobrem: persistência/dedupe/validação, isolamento da consulta pública,
negativa de permissão com auditoria, aprovação condicionada a autorização,
dry-run sem chamada de transporte, as três travas do envio real, idempotência,
intervalo mínimo, teto de lote, falha de transporte, trilha de auditoria e CLI.

## Implantação

Empacotado para Docker (`Dockerfile` + `docker-compose.yml`), com fuso
`America/Bahia`, SQLite em volume persistente e backup automático. Nenhuma
credencial no repositório: tudo vem de `.env` (modelo em `.env.example`).

```bash
cp .env.example .env          # preencha BOT_TOKEN / TARGET_CHAT_ID / ADMIN_IDS
python3 scripts/preflight.py --mode prod --scan-repo
docker compose config         # valida a sintaxe, não sobe nada
docker compose up -d          # bot + agendador
docker compose exec bot python scripts/healthcheck.py
```

Dois serviços de longa duração e dois sob demanda (`--profile tools`):

| Serviço | Comando | Papel |
|---|---|---|
| `bot` | `python -m bot.app` | long-polling; sem porta publicada |
| `scheduler` | `python scripts/scheduler.py` | backup, integridade, relatório de pendentes e coleta |
| `admin` | `python -m bot.admin …` | CLI administrativo |
| `backup` | `python scripts/backup.py …` | backup avulso |

Estado persistente em volumes nomeados: `agenda-cultural-data` (`/app/data`) e
`agenda-cultural-backups` (`/app/backups`, `.db.gz` + `.sha256`, retenção 14).

Agendamento (`config/schedule.json`, horários de Salvador): backup 03:15,
integridade 03:50, coleta em lote às 06:00, 12:00, 17:00 e 22:00 (grava
pendente/rejeitado, nunca publica) e relatório de pendentes 08:00. A janela
editorial solicitada (07:00, quinta e domingo) está declarada mas
**bloqueada**: o agendador nunca publica. Ver `docs/scheduling.md`.

Backup e restauração:

```bash
docker compose --profile tools run --rm backup create
docker compose --profile tools run --rm backup verify /app/backups/agenda-<stamp>.db.gz
docker compose --profile tools run --rm backup restore \
  --file /app/backups/agenda-<stamp>.db.gz --to /app/data/agenda.db \
  --force --confirm RESTAURAR        # exige o bot parado
```

Verificação de implantação (offline, não implanta): `scripts/deploy_check.sh`.
CI em `.github/workflows/ci.yml` (3.11/3.12/3.13 + artefatos + build).

Documentação: [docs/deploy.md](docs/deploy.md) (passo a passo, upgrade,
rollback, diagnóstico), [docs/operations.md](docs/operations.md) (runbook),
[docs/security.md](docs/security.md) (credenciais e travas),
[docs/scheduling.md](docs/scheduling.md) (agendador),
[docs/coletores-plataformas.md](docs/coletores-plataformas.md) (plataformas de ingresso)
e [docs/coletores-ufba.md](docs/coletores-ufba.md) (UFBA e fontes independentes).

## Limitações conhecidas

- **Long-polling apenas.** Sem webhook, sem TLS próprio, sem escalonamento
  horizontal; o throttle é em memória e reinicia com o processo.
- **Um processo.** SQLite em WAL aguenta bem, mas não há fila distribuída; dois
  processos publicando ao mesmo tempo podem furar o intervalo mínimo.
- **Sem envio de mídia/arquivos**, sem edição ou exclusão de mensagens já
  publicadas (correção exige novo evento e novo aviso manual).
- **Datas são horário local sem fuso** (`naive`): não há conversão de fuso nem
  horário de verão; a checagem de "evento no passado" usa UTC como aproximação.
- **Sem publicação agendada**: existe agendador (`scripts/scheduler.py`), mas
  ele roda apenas backup, integridade, relatório e coleta (esta última grava
  pendente/rejeitado). Publicar continua sendo comando humano com autorização
  registrada e frase de confirmação.
- **Sem confirmação em dois passos para exclusão/edição** — operações
  destrutivas não existem; rejeição é reversível apenas por novo estado.
- **Texto puro**: sem formatação rica, sem links inline; URLs aparecem como texto.
- **Sem i18n**: mensagens em português, datas em pt-BR fixo.
- **Banco não criptografado**: a referência de autorização fica em claro no
  SQLite; proteja o arquivo por permissão de sistema.
- **Não validado contra a API real do Telegram** (por exigência de não usar
  conexão externa); `HttpTransport` segue a Bot API e não foi exercitado em rede.
- **`/lote` não retoma** itens bloqueados: eles permanecem `approved` e voltam na
  próxima execução.
