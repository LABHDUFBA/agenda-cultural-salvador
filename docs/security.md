# Segurança da implantação

O modelo de segurança do código (identidade pelo `telegram_id`, autorização no
servidor, dry-run, auditoria) está descrito no `README.md`. Este documento
cobre a **implantação**: o que o servidor precisa garantir para que aquele
modelo valha na prática.

## 1. Credenciais

| Regra | Como é garantido |
|---|---|
| Nenhuma credencial no repositório | `.gitignore` + `.dockerignore` excluem `.env`; `scripts/preflight.py --scan-repo` varre os arquivos versionados |
| Nenhuma credencial na imagem | `Dockerfile` não copia `.env`; `.dockerignore` o remove do contexto de build |
| Nenhuma credencial em log | o token nunca é impresso, nem parcialmente; `preflight` mostra só presença e formato |
| Nenhuma credencial em `docker-compose.yml` | só `env_file: .env` |

O único segredo de produção é `BOT_TOKEN`. Ele é lido uma vez no start
(`bot.config.Settings.from_env`) e usado na `Authorization` da Bot API.
Rotação: BotFather → *Revoke current token* → atualizar `.env` →
`docker compose up -d bot`. Sem rebuild, sem perda de dados.

Permissões no host:

```bash
chmod 600 .env
chmod 700 data backups
```

Se o `.env` for comprometido, considere todos os dados de `audit_log` como
públicos: o arquivo não é criptografado (o SQLite do projeto não usa SQLCipher).

## 2. Superfície de rede

- O bot **não escuta porta**: long-polling é saída HTTPS para
  `api.telegram.org`. Não há firewall de entrada a configurar.
- Nenhum serviço do compose publica `ports:` (testado em
  `tests/deploy/test_artifacts.py`).
- Consequentemente não há webhook, TLS próprio, nem endpoint administrativo
  exposto. Todo acesso administrativo é (a) comando de chat autorizado por
  perfil ou (b) `docker compose run` no host.
- `TELEGRAM_API_BASE` só deve apontar para a API oficial ou um espelho sob seu
  controle — ele recebe o token no cabeçalho.

## 3. Endurecimento do container

Aplicado a `bot` e `scheduler` no `docker-compose.yml`:

| Configuração | Efeito |
|---|---|
| `user 10001` (build) | processo sem privilégio, sem shell de login |
| `read_only: true` | FS raiz imutável; nada persiste em `/app` fora dos volumes |
| `tmpfs /tmp` 16 MB | arquivos temporários de backup/restore sem tocar o disco |
| `cap_drop: ALL` | nenhuma capability Linux |
| `no-new-privileges` | setuid/binários não escalam |
| `restart: unless-stopped` + `healthcheck` | reinício automático com verificação real do banco |
| limites de cpu/mem | falha previsível em vez de derrubar o host |

O volume de dados é gravado pelo UID 10001. Se montar um diretório do host
(bind mount), ajuste o dono: `chown -R 10001:10001 ./data`.

## 4. Travas de publicação (o que a implantação não pode afrouxar)

1. `AGENDA_ALLOW_EXECUTE=0` é o padrão de `.env.example` e do compose. Mantenha
   assim até a validação em dry-run terminar.
2. Mesmo com `1`, o envio exige, por comando: admin no banco + evento
   `approved` com autorização confirmada + a frase exata `confirmar PUBLICAR`
   digitada (a palavra `PUBLICAR` sozinha é recusada) + `--execute`. Quatro
   condições independentes.
3. O throttle (`AGENDA_MIN_INTERVAL`) e o teto (`AGENDA_MAX_PER_RUN`) são
   **em memória**: reiniciar o processo zera o intervalo. Por isso a regra é
   um único processo `bot` (o compose não permite escala acidental de réplica
   com SQLite local).
4. Nenhuma tarefa agendada publica. O `scheduler` roda backup, integridade,
   relatório e coleta — nunca `publisher.publish`. A coleta só grava
   `pendente`/`rejeitado` pelo núcleo canônico. Três barreiras independentes:
   `publish` não está no registro executável `TASKS` (só em
   `DECLARABLE_TASKS`); `load_schedule` recusa a agenda inteira se um job
   `publish` vier com `"enabled": true` (exit `2`, antes de executar nada); e
   `run_task` devolve `4` mesmo para um `Job` construído à mão. A janela
   07:00 de quinta e domingo em `config/schedule.json` é **declaração** para a
   operação enxergar o calendário editorial, não gatilho.
5. O estado do agendador (`data/scheduler_state.json`) e o `.lock` são criados
   com modo `0600`, e o heartbeat carrega o SHA-256 da agenda. Use
   `healthcheck.py --scheduler --schedule config/schedule.json` para detectar
   agenda editada sem restart (deriva de configuração).

## 5. Dados e privacidade

- O banco guarda `telegram_id`, `username`, textos de eventos e a referência da
  autorização do organizador (ex.: "e-mail da produtora, 01/11/2026"). Isso é
  dado pessoal de terceiros: mantenha o volume restrito e os backups tratados
  como dados sensíveis.
- Backups são `.db.gz` **sem criptografia**. Se a cópia off-host sair do
  servidor, cifre o transporte/arquivo (`age`, `gpg`) — o script não faz isso.
- Retenção padrão: 14 arquivos (`AGENDA_BACKUP_KEEP`). Ajuste conforme a
  política de retenção aplicável (LGPD: minimize).
- `audit_log` é append-only pela aplicação, mas o arquivo SQLite é editável por
  quem tem acesso ao volume. Trilha confiável exige cópia off-host frequente.

## 6. Verificação contínua

```bash
python3 scripts/preflight.py --mode prod --scan-repo   # antes de cada deploy
scripts/deploy_check.sh                                # offline, inclui testes
```

`tests/deploy/test_security.py` falha se:

- `.env` (ou variante que não `.env.example`) for versionado;
- algum arquivo versionado casar com o formato de token do Telegram;
- o preflight deixar de detectar token malformado;
- o preflight passar a imprimir o token na saída.

## 7. Riscos residuais (aceitos, documentados)

- **Long-polling sem webhook**: um único processo; indisponibilidade durante
  restart. Não há fila de updates persistida.
- **Throttle em memória**: reinício pode permitir dois envios próximos.
- **SQLite no mesmo host**: perda do disco = perda da verdade, mitigada por
  cópia off-host manual.
- **Sem rate limit por usuário além do `Throttle` do bot**: flood de mensagens
  pode consumir CPU; mitigado pelos limites de container.
- **Backups não cifrados** e **sem verificação automática de restore**: o
  drill semanal é procedimento humano.
