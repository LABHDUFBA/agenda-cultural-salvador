# Operação (runbook)

Rotinas para quem mantém o serviço no ar. Nada aqui publica evento: publicação
continua sendo decisão humana (ver `security.md`).

## Rotina diária (5 min)

```bash
docker compose ps
docker compose exec bot python scripts/healthcheck.py --json
docker compose exec scheduler python scripts/healthcheck.py --scheduler --json
docker compose logs --since 24h bot | grep -Ei "error|traceback" | tail -20
docker compose --profile tools run --rm backup list
```

Esperado:

- `bot` e `scheduler` em `healthy` (ou `running` com healthcheck ok);
- `healthcheck.py` → `"ok": true`;
- `--scheduler` → `"age_seconds"` < 120 (heartbeat a cada 30 s);
- um backup do dia em `/app/backups`, com sidecar `.sha256`.

## Agenda automática

Definida em `config/schedule.json` (horários em `America/Bahia`):

| Job | Dias | Horário | O que faz |
|---|---|---|---|
| `backup-diario` | todos | 03:15 | snapshot online + verificação + retenção de 14 |
| `integridade-banco` | todos | 03:50 | `PRAGMA quick_check` + contagem por status |
| `coleta-06h/12h/17h/22h` | todos | 06:00, 12:00, 17:00, 22:00 | rotina de coleta em lote; grava pendente/rejeitado; único job com rede |
| `relatorio-pendentes` | todos | 08:00 | lista eventos em `pending_review`/`approved` no log |
| `janela-publicacao-07h` | qui, dom | 07:00 | **declaração bloqueada**; não executa |

A rotina de coleta roda quatro vezes por dia (`scripts/coleta.py`), percorre as
fontes **coletáveis** do catálogo e, com `--gravar`, persiste sempre como
`pendente` ou `rejeitado` — nunca aprova e nunca publica. Falha de uma fonte é
registrada por fonte no JSON do log e não interrompe as outras.

A janela editorial de publicação (07:00 — quinta e domingo) existe só para
documentar o calendário pedido. Vem `"enabled": false`, e a validação recusa a
agenda se alguém tentar habilitá-la: **publicação é sempre comando humano**.
Ver `docs/security.md`, seção 4.

Para suspender a coleta, troque `"enabled": true` por `false` nos quatro jobs
`coleta-*` de `config/schedule.json`, rode
`python scripts/scheduler.py --print-plan` e reinicie o serviço `scheduler`.
Antes de ligar/religar, valide as fontes com `python -m collectors.cli probe`
(uma requisição por fonte, respeitando robots.txt).

O `preflight` avisa `schedule.network` enquanto houver job de coleta
habilitado: é informativo (a coleta é a única tarefa com rede), não erro.

Confirmar o que vai rodar (sem executar):

```bash
docker compose exec scheduler python scripts/scheduler.py --print-plan
docker compose exec scheduler python scripts/scheduler.py --print-plan --json
```

Conferir se o processo em execução carregou a agenda que está no disco:

```bash
docker compose exec scheduler python scripts/healthcheck.py --scheduler \
  --schedule config/schedule.json --json
```

Divergência de `schedule_digest` significa "editei a agenda e não reiniciei".

Detalhes de configuração, dias, catch-up e trava de instância: `scheduling.md`.

## Semanal (15 min)

1. **Drill de restauração** (obrigatório — backup nunca testado não é backup):

   ```bash
   docker compose exec scheduler sh -lc '
     python scripts/backup.py verify /app/backups/$(ls /app/backups | grep "db.gz$" | tail -1) &&
     python scripts/backup.py restore \
       --file /app/backups/$(ls /app/backups | grep "db.gz$" | tail -1) \
       --to /tmp/drill.db --confirm RESTAURAR &&
     python scripts/healthcheck.py --db /tmp/drill.db --json'
   ```

   O restore escreve em `/tmp` de propósito: nada é sobrescrito em produção.

2. **Cópia off-host** dos backups. O volume protege contra `docker rm`, não
   contra perda do disco:

   ```bash
   docker run --rm -v agenda-cultural-backups:/b -v "$PWD/out":/out alpine \
     tar czf /out/backups-$(date -u +%Y%m%d).tgz -C /b .
   ```

3. Revisar `audit_log` de negativas inesperadas:

   ```bash
   docker compose run --rm admin events --status rejected
   ```

## Mensal

- Conferir crescimento do banco (`size_bytes` no healthcheck) e o número de
  arquivos em `/app/backups` (deve ser ≤ `AGENDA_BACKUP_KEEP`).
- Rotacionar o token do bot (BotFather → revoke → atualizar `.env` →
  `docker compose up -d bot`). Não requer rebuild: token é ambiente.
- Revisar quem é admin: `docker compose run --rm admin admin list`.
- Rodar `scripts/deploy_check.sh` na versão em produção (a partir do commit
  implantado) para detectar desvio de configuração.

## Incidentes

### Bot parado / unhealthy

```bash
docker compose ps
docker compose logs --tail=200 bot
docker compose exec bot python scripts/healthcheck.py --json
```

- banco ausente → conferir volume `agenda-cultural-data`;
- `integrity` ≠ `ok` → **não** reinicie em loop: pare, restaure o último
  backup verificado (abaixo);
- token inválido (`401`) → rotacionar no BotFather e atualizar `.env`.

### Banco corrompido

```bash
docker compose stop bot scheduler
docker compose --profile tools run --rm backup list
docker compose --profile tools run --rm backup verify /app/backups/<arquivo>.db.gz
docker compose --profile tools run --rm backup restore \
  --file /app/backups/<arquivo>.db.gz --to /app/data/agenda.db \
  --force --confirm RESTAURAR
docker compose start bot scheduler
docker compose exec bot python scripts/healthcheck.py --json
```

Perda esperada = eventos/estados criados depois do snapshot restaurado. O
restore apaga `-wal`/`-shm` antigos de propósito: manter esses arquivos ao lado
de um banco substituído corrompe o resultado.

### Scheduler sem rodar

```bash
docker compose logs --tail=200 scheduler
docker compose exec scheduler python scripts/scheduler.py --print-plan
```

Causas comuns: `config/schedule.json` inválido (exit 2, mensagem explícita),
trava de instância órfã (`state.json.lock` de processo que morreu — o `flock`
é liberado pelo kernel, então normalmente não trava), permissão no volume.

### Disco cheio

1. `docker compose --profile tools run --rm backup prune --keep 7` (ou ajustar
   `AGENDA_BACKUP_KEEP`);
2. `docker system prune` na imagem antiga;
3. nunca apagar o `.db` para "liberar espaço".

### Publicação em curso indesejada

```bash
# desliga a possibilidade de envio real imediatamente
sed -i 's/^AGENDA_ALLOW_EXECUTE=.*/AGENDA_ALLOW_EXECUTE=0/' .env
docker compose up -d bot
```

O `Publisher` revalida a flag a cada chamada, então a trava vale no próximo
comando — não há fila em background enviando sozinha.

## Comandos de referência

```bash
docker compose run --rm admin admin list             # perfis no banco
docker compose run --rm admin events --status pending_review
docker compose run --rm admin event <id>             # detalhe + bloqueios
docker compose run --rm admin audit <id>             # trilha do evento
docker compose --profile tools run --rm backup create
docker compose --profile tools run --rm backup verify /app/backups/<arquivo>
```

Comandos de chat (filtros, semântica de data/gratuidade, `/corrigir` e
`/atualizar`): ver [`comandos.md`](comandos.md).

## Limites conhecidos da operação

- **Um escritor**: SQLite em WAL aceita leitores concorrentes, mas dois
  processos publicando podem furar o intervalo mínimo (throttle em memória).
  Não escale `bot` para mais de uma réplica.
- **Sem alta disponibilidade**: reinício do container = indisponibilidade de
  segundos; o long-polling reconecta sozinho.
- **Sem métricas Prometheus** nem tracing; observabilidade = logs JSON-ish +
  healthcheck + estado do agendador.
- **Backups ficam no mesmo host** por padrão; a cópia off-host do checklist
  semanal é manual e continua sendo responsabilidade da operação.
