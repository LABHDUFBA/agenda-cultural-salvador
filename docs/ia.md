# Integração opcional de IA (Refuseless)

Classificação **assistida** de eventos já coletados. Existe para reduzir o
trabalho manual na fila de revisão — nunca para decidir o que entra no canal.

## Princípio

> O coletor determinístico continua sendo o caminho principal. A IA é um
> enfeite auditável em cima dele.

Três consequências práticas:

1. **Desligada por padrão.** Sem `AGENDA_IA_ENABLED=1` **e**
   `REFUSELESS_API_KEY`, nenhum código de rede deste pacote roda. `pytest`,
   `demo_offline.py`, Docker e CI funcionam exatamente como antes.
2. **A sugestão não é decisão.** `ia_sugestoes` é uma tabela separada; nada
   escreve `eventos.status` a partir dela. Aprovar continua sendo
   `bot/admin.py aprovar` (ou `/aprovar` no bot), por um administrador.
3. **Sem proveniência não há sugestão.** Cada linha guarda o hash SHA-256 do
   texto exato enviado, a URL da fonte e o trecho de evidência que o coletor
   capturou. Uma sugestão órfã de fonte não é auditável e não é gravada.

## O que a IA pode opinar (e o que não pode)

| Pode (metadado de classificação) | Não pode, em nenhuma hipótese |
| --- | --- |
| `categoria` (vocabulário fechado) | `status`, aprovação, publicação |
| `publico`, `faixa_etaria` | `municipio`, `uf` |
| `gratuito` | `local_nome`, `endereco`, coordenadas |
| `acessibilidade`, `tags`, `resumo` | `inicio_em`, `fim_em` |
| `confianca` + `justificativa` | `url_fonte`, `url_original` |
| `campos_incertos` | `autorizacao_confirmada`, `autorizacao_referencia` |

`ia/esquema.py` mantém a lista em `CAMPOS_PROIBIDOS`. Se o modelo devolver
qualquer um deles, o valor é registrado no JSON bruto (para auditoria da
tentativa) e **descartado** — não existe campo correspondente em `SugestaoIA`.

O único campo canônico que pode ser preenchido é `categoria`, e só por
`ia/cli.py aceitar --ator <nome>`, em evento `pendente`, com trilha em
`auditoria` (`acao='ia_sugestao_aceita'`, estado anterior e novo).

## Configuração

| Variável | Padrão | Efeito |
| --- | --- | --- |
| `AGENDA_IA_ENABLED` | `0` | intenção explícita de ligar |
| `REFUSELESS_API_KEY` | vazio | credencial (só ambiente; nunca versionada) |
| `AGENDA_IA_BASE_URL` | `https://api.refuseless.com/v1` | base compatível com OpenAI; `http://` só em loopback |
| `AGENDA_IA_MODEL` | `deepseek-v4.1-flash-abliterated` | modelo usado |
| `AGENDA_IA_TIMEOUT` | `20` | segundos, 1..120 |
| `AGENDA_IA_MAX_CHAMADAS_LOTE` | `3` | teto por lote de coleta, 1..10 |
| `AGENDA_IA_MAX_CHAMADAS_DIA` | `20` | teto diário (UTC), 1..500 |
| `AGENDA_IA_TEMPERATURA` | `0.0` | 0..1 |

`habilitada` só é `True` com a flag **e** a chave. A chave fica em campo
`repr=False`, nunca aparece em `resumo()`, em log, em mensagem de erro
(`sanitizar()`) nem em saída de CLI.

## Uso parcimonioso

Somar tetos é mais barato que confiar em disciplina:

* **elegibilidade** — só evento com `categoria` vazia ou `confianca='baixa'`
  consome chamada (`ia/enriquecimento.py:motivo_elegibilidade`);
* **teto por lote** — `AGENDA_IA_MAX_CHAMADAS_LOTE`, contado em memória;
* **teto diário** — `AGENDA_IA_MAX_CHAMADAS_DIA`, contado em `ia_chamadas`,
  então vale entre execuções e entre processos;
* **cache por hash** — a mesma entrada já classificada não custa outra
  chamada; o acerto é registrado como `resultado='cache'`;
* **resposta curta** — `max_tokens=480`, `temperature=0`, prompt v2 com
  justificativa de até 20 palavras (v1 gastava ~400 tokens por resposta);
* **entrada mínima** — título, local, preço, categoria declarada, descrição e
  trecho da fonte (máx. 1200 caracteres). A página crua nunca é enviada.

`ia_chamadas` registra **toda** tentativa (`ok`, `erro`, `cache`, `limite`),
inclusive as que falharam. O consumo é auditável, não declarado:

```bash
python -m ia.cli consumo --db data/agenda.db
```

## Fail-safe

Qualquer falha — timeout, HTTP 4xx/5xx, JSON inválido, campo fora do
vocabulário, exceção inesperada — vira contador (`falhas`) no relatório e
detalhe sanitizado. O lote já foi persistido antes da etapa de IA
(`collectors/ingest.py`), e o evento continua `pendente` com exatamente os
campos que o coletor produziu. Não há retry automático: uma falha custa uma
tentativa e para.

## Fluxo

```
coletor (determinístico)
   -> ingerir_evento -> eventos.status = 'pendente' | 'rejeitado'
      -> [se --ia e habilitada] EnriquecimentoIA.processar_lote(pares)
         -> elegível? -> cache por hash? -> chamada única
            -> validação estrita -> ia_sugestoes (status='sugerida')
                     |
                     +-> moderador lê em fila_de_revisao() / ia.cli listar
                         -> ia.cli aceitar --ator <nome>  (só 'categoria')
                         -> ia.cli descartar --ator <nome>
                         -> aprovar/publicar: fora deste pacote, sempre humano
```

## Comandos

```bash
# configuração, sem rede e sem custo
python -m ia.cli verificar

# UMA chamada real (custa token): prova de integração
python -m ia.cli verificar --executar

# coleta com enriquecimento (exige AMBAS as variáveis)
python scripts/coleta.py --gravar --ia --fontes tca --limite 10 --json

# revisão
python -m ia.cli listar --db data/agenda.db --status sugerida
python -m ia.cli aceitar --db data/agenda.db --id 12 --ator curador
python -m ia.cli descartar --db data/agenda.db --id 12 --ator curador
python -m ia.cli consumo --db data/agenda.db --json
```

`coleta.py --ia` sem `--gravar` sai com código 2, e `--ia` com a integração
desligada também sai com 2 — em vez de rodar "sem IA" em silêncio.

## Tabelas (schema v4, aditivo)

* `ia_sugestoes` — sugestão + proveniência da entrada (`hash_entrada` de 64
  hex, `url_original`, `trecho_entrada`, `modelo`, `prompt_versao`),
  `confianca`, `campos_incertos`, `status` (`sugerida|aceita|descartada`).
  `CHECK` exige `decidida_em` e `decidida_por` fora de `sugerida`.
* `ia_chamadas` — tentativa, resultado, duração. Base do teto diário.

Nenhuma tabela ou coluna existente foi alterada; `PRAGMA user_version` vai a 4.

## Limites conhecidos

* A classificação é opinião de um modelo: `confianca < 0,6` ou
  `campos_incertos` não vazio marcam `revisao_obrigatoria` na sugestão.
* `resumo`/`tags` não são gravados em `eventos` (não há coluna canônica para
  eles); aparecem na revisão e em `ia_sugestoes`.
* Um provedor fora do ar reduz a classificação a zero, sem afetar a coleta.
* O prompt é versionado (`PROMPT_VERSAO`) e gravado em cada linha; mudar o
  prompt exige nova versão para que sugestões antigas continuem interpretáveis.
