# Implantação

Guia para colocar o Agenda Cultural Salvador em um servidor. Tudo aqui é
reproduzível a partir do repositório: **nenhuma credencial é versionada** e a
implantação começa em modo seguro (publicação em dry-run).

- Runtime: Python 3.12 (imagem `python:3.12-slim`), apenas biblioteca padrão.
- Estado: SQLite com WAL em volume persistente.
- Fuso: `America/Bahia` (horário de Salvador, sem horário de verão desde 2019).
- Rede: o bot faz **long-polling** (saída HTTPS para `api.telegram.org`).
  Não há porta publicada, não há webhook, não há TLS próprio.

## 1. Pré-requisitos

| Item | Versão | Observação |
|---|---|---|
| Docker Engine | 24+ | `docker compose config` precisa funcionar |
| Docker Compose | v2.24+ | usa `name:`, `profiles:` e `env_file: required: false` |
| Git | qualquer | deploy por clone + `compose build` |
| Espaço em disco | ~1 GB | imagem + banco + 14 backups comprimidos |

O servidor precisa de saída HTTPS para `api.telegram.org`. Se houver proxy,
ele deve ser configurado no host (o código não lê variáveis de proxy).

## 2. Primeira implantação

`docker-compose.yml` declara `.env` como **opcional**
(`env_file: required: false`) e traz padrões seguros no próprio arquivo:
sem `.env`, `docker compose config` continua válido e a pilha sobe em modo
seguro — `BOT_TOKEN` vazio faz o bot sair com código 2 (fail-closed, o
`restart: unless-stopped` vai insistir até você criar o `.env`) e a publicação
segue bloqueada por `AGENDA_ALLOW_EXECUTE=0`. O compose nunca contém valor de
credencial, apenas referências `${VAR:-}`; por isso um `.env` ausente não
impede validar a configuração, mas **é obrigatório para operar de verdade**.

```bash
git clone <repo> agenda-cultural-salvador
cd agenda-cultural-salvador

cp .env.example .env
$EDITOR .env                     # BOT_TOKEN, TARGET_CHAT_ID, ADMIN_IDS

# 1) credenciais e caminhos, sem subir nada
python3 scripts/preflight.py --mode prod --scan-repo

# 2) sintaxe do compose (não precisa de daemon)
docker compose config

# 3) build + subida
docker compose build
docker compose up -d

# 4) conferência
docker compose ps
docker compose logs --tail=50 bot
docker compose exec bot python scripts/healthcheck.py
docker compose exec scheduler python scripts/healthcheck.py --scheduler
```

### Variáveis mínimas de produção

| Variável | Valor | Efeito se ausente |
|---|---|---|
| `BOT_TOKEN` | `<id>:<segredo>` | o processo **não inicia** (exit 2) |
| `TARGET_CHAT_ID` | `-100…` para canal | publicação real fica bloqueada |
| `ADMIN_IDS` | ids separados por vírgula | ninguém consegue aprovar/publicar |
| `TZ` | `America/Bahia` | horários e agenda deslocados |
| `AGENDA_ALLOW_EXECUTE` | `0` (padrão) | envio real impossível |

`AGENDA_ALLOW_EXECUTE=1` é o kill switch de implantação: mesmo ligado, cada
envio exige `--execute` **e** a frase `PUBLICAR` digitada por um admin.
Recomendação: subir com `0`, validar o fluxo em dry-run, e só então liberar.

## 3. O que sobe

| Serviço | Comando | Papel |
|---|---|---|
| `bot` | `python -m bot.app` | long-polling; responde consulta e administração |
| `scheduler` | `python scripts/scheduler.py` | backup, integridade, coleta (06/12/17/22) e relatório de pendentes |
| `admin` | `python -m bot.admin` | CLI, perfil `tools`, sobe só sob demanda |
| `backup` | `python scripts/backup.py` | backup avulso, perfil `tools` |

Volumes nomeados (persistem entre `up`/`down` e rebuilds):

- `agenda-cultural-data` → `/app/data` (SQLite + estado do agendador)
- `agenda-cultural-backups` → `/app/backups` (`.db.gz` + `.sha256`)

```bash
docker compose run --rm admin events --status pending_review
docker compose --profile tools run --rm backup create
```

> `docker compose down` **não** apaga volumes. `docker compose down -v` apaga:
> use apenas quando a intenção for descartar dados, e depois de um backup
> verificado fora do host.

## 4. Inicialização do banco

O schema é criado na primeira conexão (`bot.app` e `bot.admin` inicializam).

```bash
docker compose run --rm admin init --admin 123456789
docker compose run --rm admin admin list
```

Admins do `ADMIN_IDS` também são promovidos no start. A fonte de verdade é a
tabela `users` no SQLite — não o ambiente.

## 5. Atualização (deploy de nova versão)

```bash
cd agenda-cultural-salvador
git fetch --all && git checkout <tag-ou-commit>

# backup ANTES de trocar código (com o serviço atual ainda rodando)
docker compose --profile tools run --rm backup create
docker compose --profile tools run --rm backup verify /app/backups/agenda-<stamp>.db.gz

docker compose build
docker compose up -d
docker compose exec bot python scripts/healthcheck.py
docker compose logs --tail=100 bot scheduler
```

Rollback:

```bash
git checkout <commit-anterior>
docker compose build && docker compose up -d
# se a versão nova migrou o schema de forma incompatível, restaure o backup:
docker compose stop bot scheduler
docker compose --profile tools run --rm backup restore \
  --file /app/backups/agenda-<stamp>.db.gz --to /app/data/agenda.db \
  --force --confirm RESTAURAR
docker compose up -d
```

O `database/connection.py` recusa abrir um banco com `user_version` maior que
o suportado — um rollback de código sobre schema novo falha de forma explícita
em vez de corromper dados.

## 6. Verificação pós-deploy

```bash
scripts/deploy_check.sh              # offline: compila, preflight, testes, compose
docker compose ps                    # bot e scheduler "healthy"
docker compose exec bot python scripts/healthcheck.py --json
docker compose exec scheduler python scripts/healthcheck.py --scheduler --json
```

Checklist mínimo antes de liberar o canal:

- [ ] `docker compose ps` mostra os dois serviços `healthy`
- [ ] `healthcheck.py` → `"ok": true`, `event_table` preenchido
- [ ] `healthcheck.py --scheduler` → `ok`, `age_seconds` < 120
- [ ] `docker compose logs bot` sem traceback e com `dry-run=true` se `AGENDA_ALLOW_EXECUTE=0`
- [ ] `/status` respondendo no Telegram para um admin
- [ ] primeiro backup do dia presente em `/app/backups` com `.sha256`

## 7. Bare metal (sem Docker)

O projeto não exige container. O mínimo é Python 3.10+ e um supervisor:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
set -a && . ./.env && set +a
.venv/bin/python -m bot.admin init --admin <seu_id>
.venv/bin/python -m bot.app                 # um supervisor mantém vivo
.venv/bin/python scripts/scheduler.py       # outro supervisor mantém vivo
```

Sem container você perde o isolamento (usuário, `read_only`, `cap_drop`) e as
travas de recurso. Se possível, rode como usuário dedicado, com o diretório de
dados em `chmod 700`, e mantenha `scripts/healthcheck.py` no seu monitoramento.

## 8. Diagnóstico rápido

| Sintoma | Causa provável | Ação |
|---|---|---|
| `BOT_TOKEN ausente: o bot não inicia` | `.env` vazio ou não carregado | conferir `docker compose config` e reiniciar |
| container `unhealthy` no bot | banco ausente/corrompido | `healthcheck.py --json`, depois `operations.md` |
| `database is locked` | escritor concorrente de longa duração | verificar `busy_timeout`, evitar dois backups simultâneos |
| nada é publicado | `AGENDA_ALLOW_EXECUTE=0` ou falta frase/autorização | é o comportamento esperado; ver `security.md` |
| `getUpdates` falhando | rede/proxy/token revogado | testar `curl -s https://api.telegram.org` no host |
| scheduler sem heartbeat | processo morto ou em crash loop | `docker compose logs scheduler`; healthcheck `--scheduler` |
| imagem não constrói | sem rede para o registry | build em máquina com cache/push para registry interno |
