# Agendamento

O agendador (`scripts/scheduler.py`) roda tarefas **declaradas** em horários
locais de Salvador: manutenção (backup, integridade, relatório) e a rotina de
coleta. Ele existe para responder "o backup rodou?" e "a coleta das 06 h
aconteceu?" sem depender de memória humana — não para publicar conteúdo.

## O que é agendado (e o que não é)

| Job | Tarefa | Dias | Horário | Efeito |
|---|---|---|---|---|
| `backup-diario` | `backup` | todos | 03:15 | snapshot `.db.gz` + `.sha256`, retenção 14 |
| `integridade-banco` | `integrity` | todos | 03:50 | `quick_check` + contagem por status |
| `coleta-06h` | `coleta` | todos | 06:00 | coleta em lote; grava pendente/rejeitado |
| `coleta-12h` | `coleta` | todos | 12:00 | idem |
| `coleta-17h` | `coleta` | todos | 17:00 | idem |
| `coleta-22h` | `coleta` | todos | 22:00 | idem |
| `relatorio-pendentes` | `pending_report` | todos | 08:00 | lista pendentes no log (limite 20 linhas) |
| `janela-publicacao-07h` | `publish` | qui, dom | 07:00 | **declaração bloqueada** — não executa |

São 8 jobs. Sete são executáveis; **um** é apenas declaração editorial.

## Rotina de coleta (06, 12, 17, 22 — todos os dias)

Quatro jobs `coleta`, `"enabled": true`, chamam o mesmo pipeline:

```bash
python scripts/coleta.py --json --limite 50 --gravar
```

`scripts/coleta.py` percorre as fontes **coletáveis** do catálogo
(`sources/catalog.py`: `acessibilidade == "ok"` e estrutura suportada),
uma requisição por fonte, sem disfarce de bloqueio e sem retry agressivo:

- falha de uma fonte **não** interrompe as demais — o relatório JSON diz o que
  passou e o que falhou, fonte por fonte;
- `--gravar` persiste pelo núcleo canônico (`collectors.ingest`), sempre como
  `pendente` ou `rejeitado`. A coleta **nunca** aprova e **nunca** publica;
- código de saída `0` quando ao menos uma fonte coletou (sucesso parcial é
  normal: fonte bloqueada não é bug do job), `1` quando nenhuma coletou,
  `2` para uso inválido;
- `args` aceitos na agenda: `fontes` (lista opcional de ids; padrão = todas as
  coletáveis), `limite` (1..200, padrão 50) e `gravar` (booleano, padrão
  `false`; os quatro jobs do repositório usam `true`).

Esta é a **única** tarefa agendada que abre socket. `preflight.py` emite o
aviso `schedule.network` sempre que há job de coleta habilitado — é
informativo, não erro.

Para suspender a rotina sem apagar configuração, troque `"enabled": true` por
`false` nos quatro jobs, rode `python scripts/scheduler.py --print-plan` e
reinicie o serviço `scheduler` (o healthcheck detecta deriva de agenda).

### Cobertura real vs. desativada

O que roda de fato depende do catálogo, não do agendador:

- **implementado e ativo**: coletores das fontes `coletavel` (hoje BaGov
  calendário, Salvador da Bahia, Vila Velha, Sympla, Casa Rosa, Bilheteria
  Digital);
- **declarado no catálogo, sem parser**: fontes `nao_suportada` — aparecem em
  `coleta --json` como falha explícita se forem pedidas por `--fontes`, e
  nunca são coletadas por padrão;
- **bloqueadas** (401/403/429/robots): permanecem no catálogo como dado, sem
  tentativa de contorno; ficam fora da lista padrão porque `coletavel` é
  falso.

## Janela editorial de publicação (07 — quinta e domingo)

`janela-publicacao-07h` está **declarada** em `config/schedule.json` com
`task: "publish"`, `days: ["thu", "sun"]` e `enabled: false`. Leitura adotada
para o pedido "agendas 07 e quinta/domingo": uma janela às 07:00 nos dias de
quinta e domingo. Se a intenção for 07:00 **todos os dias**, basta remover a
chave `days` do job (o agendador continua recusando habilitá-lo). Declarar
serve para a operação enxergar o calendário editorial em `--print-plan`;
**não** torna nada executável:

- `load_schedule` recusa a agenda inteira se um job `publish` vier com
  `enabled: true` (exit `2`, antes de qualquer execução);
- `run_task` recusa a tarefa mesmo que um `Job` seja construído à mão em
  memória (código `4`);
- `publish` não está no registro `TASKS` — só em `DECLARABLE_TASKS`.

**Não existe publicação automática.** Publicar é ato humano com autorização
registrada e frase de confirmação; um agendador que enviasse sozinho
contradiria esse desenho. Se um dia isso mudar, o lugar da mudança é uma
tarefa nova, explicitamente revisada — não uma flag escondida no
`schedule.json`.

## Formato do `config/schedule.json`

```json
{
  "timezone": "America/Bahia",
  "jobs": [
    {
      "name": "backup-diario",
      "task": "backup",
      "at": "03:15",
      "enabled": true,
      "args": {"keep": 14, "verify": true}
    },
    {
      "name": "coleta-06h",
      "task": "coleta",
      "at": "06:00",
      "enabled": true,
      "args": {"limite": 50, "gravar": true}
    },
    {
      "name": "janela-publicacao-07h",
      "task": "publish",
      "at": "07:00",
      "days": ["thu", "sun"],
      "enabled": false
    }
  ]
}
```

| Campo | Obrigatório | Regra |
|---|---|---|
| `timezone` | não | padrão `America/Bahia`; validado com `zoneinfo` |
| `jobs` | sim | lista não vazia |
| `name` | sim | único, `[A-Za-z0-9._-]`, é a chave do estado |
| `task` | sim | executável (`TASKS`) ou declarável (`publish`) |
| `at` | sim | `HH:MM`, 00:00–23:59 |
| `days` | não | `"*"` (padrão), `"qui,dom"`, `["thu","sun"]`, `[4,7]` — ISO 1=seg … 7=dom |
| `enabled` | não | booleano; `true` por padrão, exceto `publish` (proibido) |
| `args` | não | validado por tarefa; chave desconhecida = erro |

`days` aceita português (`seg`, `terça`, `quinta`, `domingo`) e inglês
(`mon`…`sun`), além de números ISO. Faixas como `"seg-sex"` **não** são
suportadas de propósito: lista explícita é mais fácil de auditar. Um mesmo
`task`/`at`/`days` repetido é recusado (slot duplicado).

JSON e não YAML de propósito: o runtime é biblioteca padrão, sem `PyYAML`, e um
arquivo de configuração que o próprio processo não consegue ler é um risco.

Validação antes de confiar:

```bash
python scripts/scheduler.py --print-plan        # horários, dias e próximo disparo
python scripts/scheduler.py --print-plan --json # mesma informação para máquina
python scripts/preflight.py                     # valida a agenda junto do ambiente
```

Erro de configuração → saída `2` com mensagem explícita, antes de qualquer
execução.

## Semântica de execução

- **Uma vez por slot (job + dia).** O estado (`AGENDA_SCHEDULER_STATE`, padrão
  `data/scheduler_state.json`, modo `0600`) guarda a data local **do slot**
  executado. Um slot de quinta 22 h recuperado na sexta 02 h continua sendo
  quinta e não impede o slot de sexta.
- **Quatro slots de coleta por dia**, um por horário: 06:00, 12:00, 17:00 e
  22:00 são jobs distintos, então cada um tem seu próprio registro — um deles
  falhar não bloqueia os outros.
- **Só nos dias de `days`.** Job de quinta/domingo não roda em outro dia, nem
  por catch-up.
- **Janela de catch-up de 6 h** (`AGENDA_CATCHUP_HOURS`). Se o processo estava
  parado às 03:15 e voltou às 07:00, o backup roda na hora. Se voltou às 22:00,
  o slot é marcado como perdido (`"detail": "slot perdido (catch-up
  expirado)"`) e não roda até o dia seguinte — evita backup fora de janela
  competindo com o bot. A mesma regra vale para a coleta: uma coleta de 06 h
  recuperada às 15 h já está fora da janela e é registrada como perdida, em
  vez de rodar seis horas atrasada.
- **Trava de instância única** (`flock` em `<state>.lock`, modo `0600`). Uma
  segunda instância sai com código `3` em vez de duplicar backups.
- **Heartbeat a cada tick** (`AGENDA_SCHEDULER_TICK`, 30 s), com
  `schedule_digest` (SHA-256 do arquivo de agenda) e a lista de jobs
  habilitados/bloqueados. É isso que o healthcheck do container lê:

  ```bash
  python scripts/healthcheck.py --scheduler --json
  # {"ok": true, "age_seconds": 12, "max_age_seconds": 900, "schedule_digest": "..."}
  ```

- **Detecção de deriva**: `healthcheck.py --scheduler --schedule config/schedule.json`
  compara o digest em disco com o do heartbeat. Divergência = agenda editada sem
  restart, sai `1` com o motivo — evita acreditar num plano que o processo não
  carregou.
- **SIGTERM/SIGINT** encerram entre tarefas, com código 0. O `stop_grace_period`
  do compose é 60 s por causa disso.
- **Timeout por tarefa**: `AGENDA_TASK_TIMEOUT` (1800 s). Um backup ou uma
  coleta travada vira falha registrada, não trava o agendador para sempre.
  A coleta também tem timeout de socket próprio em `collectors/fetch.py`.
- **Sem shell**: cada tarefa monta um `argv` fixo (`subprocess.run` sem
  `shell=True`). Não há interpolação de string em comando.

## Modos de execução

```bash
python scripts/scheduler.py                  # loop de longa duração (container)
python scripts/scheduler.py --once           # todos os jobs habilitados, uma vez, e sai
python scripts/scheduler.py --once --only-task backup-diario   # um job específico
python scripts/scheduler.py --once --only-due # só os slots devidos agora (dia/horário)
python scripts/scheduler.py --once --dry-run # só registra o que faria
python scripts/scheduler.py --print-plan     # inspeção
```

Atenção: `--once` sem `--only-due` executa **inclusive os quatro jobs de
coleta** (eles estão habilitados) e, como usam a rede, demora. Para rodar só
manutenção use `--only-task` (repetível) ou `--dry-run`. A CI e os testes usam
`--only-task backup-diario` justamente para não abrir socket.

`--once` é "rodar agora" e ignora dia/horário de propósito (é o que a CI e os
testes usam, e serve para "rodar o backup agora"). Para respeitar a agenda, use
`--only-due`. `--dry-run` também pode vir por `AGENDA_SCHEDULER_DRY_RUN=1`; em
dry-run nada é gravado no estado — um ensaio não move o heartbeat do container
nem mascara um run real perdido.

## Adicionar uma tarefa

1. Escreva a função no registro `TASKS` em `scripts/scheduler.py`, devolvendo um
   `argv` fixo (lista de strings) — nunca uma string de shell:
   ```python
   def task_prune(args: dict) -> list[str]:
       return [sys.executable, str(ROOT / "scripts" / "backup.py"), "prune",
               "--keep", str(int(args.get("keep", 14)))]
   TASKS["prune"] = task_prune
   ```
2. Valide `args` em `validate_args` (chave desconhecida deve falhar na leitura).
3. Acrescente o job em `config/schedule.json` com `name` único.
4. Rode `python scripts/scheduler.py --print-plan` e
   `python -m pytest tests/deploy/test_scheduler.py -q`.
5. Atualize a tabela deste documento e a de `docs/operations.md`.

Regras ao adicionar: a tarefa precisa terminar (nada de `while True`), devolver
código 0/1 significativo, ser idempotente na repetição e não depender de rede
sem tratar falha como transitória. Tarefas que publicam **não entram** em
`TASKS`.

## Fuso horário

Todo `at` é interpretado em `timezone` do arquivo, e o estado registra a data
**local do slot**. Consequências práticas:

- `America/Bahia` não tem horário de verão desde 2019; ainda assim o cálculo usa
  `zoneinfo`, então uma mudança de regra não exige alteração de código.
- Se `TZ` do container e `timezone` da agenda divergirem, os logs ficam em um
  fuso e a agenda em outro. O `preflight` avisa quando `TZ != America/Bahia`.
- Comparações internas usam `datetime` com tzinfo; não há horário "ingênuo" no
  agendador (o domínio de eventos do bot ainda usa datas sem fuso — limitação
  documentada no README).

## Por que não cron/systemd timer

`cron` no host exigiria acesso ao container, um crontab editável (superfície de
injeção) e um segundo lugar onde a configuração de fuso pode divergir. O
agendador interno mantém tudo em um arquivo versionado e testável, e ainda
produz heartbeat — o que um crontab não faz. Para bare metal, o mesmo comando
(`scripts/scheduler.py`) roda sob qualquer supervisor.
