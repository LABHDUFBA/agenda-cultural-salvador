# Comandos do bot — consulta, recortes e administração

Este documento descreve os comandos implementados em `bot/handlers.py` e a
semântica exata de cada filtro. Todos leem o **SQLite canônico**
(`database/schema.sql`) — não existe banco paralelo do bot.

Regra comum: só aparece em comando público evento com `eventos.status =
'aprovado'`. Pendente, rejeitado e duplicado nunca vazam para a consulta.
`eventos.status` é editorial; "publicado" é derivado de
`tentativas_publicacao` e não muda o que a consulta mostra.

## Consulta pública

| Comando | Janela | Coluna/filtro |
|---|---|---|
| `/hoje` | hoje, a partir de agora | `data_ref = hoje` + `inicio_em >= agora` |
| `/amanha` | amanhã inteiro | `data_ref = hoje+1` |
| `/semana` | hoje + 6 dias | `data_ref BETWEEN hoje AND hoje+6` + `inicio_em >= agora` |
| `/fimdesemana` | sábado e domingo correntes/próximos | `data_ref` do par |
| `/gratis` | próximos 30 dias | `preco` textual: gratuito/grátis/entrada franca/free/sem custo/R$ 0 |
| `/musica` | próximos 60 dias | categoria contém "música" ou palavra-chave de música |
| `/teatro` | próximos 60 dias | categoria/palavra-chave de teatro |
| `/cinema` | próximos 60 dias | categoria/palavra-chave de cinema |
| `/exposicoes` | próximos 60 dias | categoria/palavra-chave de artes visuais |
| `/noite` | próximos 7 dias | hora local de início ≥ 18h ou < 5h |
| `/ufba` | próximos 90 dias | local/endereço/descrição com UFBA ou unidade listada |
| `/bairro` | — | lista bairros aprovados com contagem |
| `/bairro nome` | próximos 90 dias | `bairro` sem acento/maiúsculas (contém) |
| `/fontes` | — | catálogo canônico `fontes` + última sessão + aprovados |
| `/agenda [n]` | a partir de agora | próximos `n` (1–20, padrão 8) |
| `/evento ID` | — | detalhe público; staff vê bloco interno |
| `/buscar termo` | — | título, local e descrição, sem acento |
| `/ajuda` | — | ajuda por perfil |

### Base de datas e fuso

`eventos.data_ref` é a data **local de Salvador** calculada na ingestão
(`database/dedup.data_ref_local`). Os comandos de janela usam essa coluna, não
`substr(inicio_em, 1, 10)` — que seria UTC e adiantaria eventos noturnos para o
dia seguinte. `hoje` vem de `America/Bahia`, nunca do fuso do servidor.

`/noite` converte `starts_at` (horário local sem fuso, como o repositório
entrega) e considera noite 18:00–04:59.

### Gratuidade

`preco` é texto livre da fonte. O bot **não infere** gratuidade:

- vazio = desconhecido → não entra em `/gratis`;
- "Gratuito", "Entrada franca", "grátis", "free", "sem custo", "sem ingresso"
  → entra;
- "R$ 0" / "R$ 0,00" → entra;
- qualquer outro valor → não entra.

A mensagem de `/gratis` diz explicitamente que a gratuidade é conforme a fonte
e deve ser confirmada no link do evento.

### Categorias (música, teatro, cinema, exposições)

Os coletores gravam `categoria` vazia hoje. Por isso o recorte combina:

1. `eventos.categoria`, quando preenchida (comparação normalizada); e
2. uma lista declarada de palavras-chave em título/descrição
   (`bot/handlers.py:PALAVRAS_CHAVE`).

É um recorte editorial explícito, não classificação semântica: um evento pode
aparecer em mais de um recorte (ex.: "musical" casa teatro) ou não aparecer se
a fonte não usar nenhuma palavra prevista. Ampliar a lista é a forma de
corrigir sem tocar em collectors.

### UFBA

Casa `ufba`, `universidade federal da bahia`, `escola de musica`,
`escola de belas artes`, `instituto de letras`, `reitoria`,
`campus de ondina`, `pavilhao de aulas`, `teatro do movel` em
título/local/endereço/descrição. Não é vínculo institucional verificado; é
recorte textual declarado.

### Bairro

Lista e filtro usam `eventos.bairro`, que a coleta pode deixar vazio. Se nenhum
evento aprovado tem bairro, o comando explica isso em vez de inventar
agrupamento. O filtro é aplicado em Python (sem acento/maiúsculas) porque o
schema canônico não tem `bairro_normalizado`; o recorte de datas continua no
SQL e o universo é limitado aos 500 primeiros eventos do período.

### Fontes (`/fontes`)

Somente leitura de `fontes`, com `confiabilidade`, `url_base`, data da última
`iniciada_em` em `sessoes` e contagem de eventos aprovados **com proveniência
daquela fonte**. Evento criado por `/novo` não conta para fonte nenhuma, porque
não tem proveniência — a mesma regra do portão de publicação.

## Administração

| Comando | Efeito |
|---|---|
| `/pendentes` | fila `pendente` com estado de autorização |
| `/autorizar ID referência` | grava autorização do organizador |
| `/aprovar ID` | `pendente → aprovado` (exige autorização) |
| `/rejeitar ID motivo` | `rejeitado` + motivo na auditoria |
| `/publicar ID [confirmar PUBLICAR]` | dry-run por padrão; execução só com flag e frase |
| `/lote [confirmar PUBLICAR]` | simula/executa a fila aprovada |
| `/auditoria ID` | trilha append-only do evento |
| `/corrigir ID campo=valor` | corrige **um** campo editorial |
| `/duplicados` | registros históricos com `status='duplicado'` |
| `/atualizar` | frescor da coleta por fonte — não acessa a rede |
| `/status` | contagens e estado do publisher |
| `/usuarios`, `/papel ID perfil` | perfis |

### `/corrigir`

Formato: `/corrigir 42 preco=Entrada franca` — um campo por comando, valor é o
resto da mensagem (aceita espaços).

Campos: `titulo`, `local`, `inicio`, `fim`, `descricao`, `url`, `bairro`,
`categoria`, `preco`, `endereco`.

- Exige perfil `admin` (`event.correct` é auditado quando negado).
- `status`, autorização e proveniência **não** são corrigíveis por aqui.
- `titulo`, `local` e `inicio` recalculam `titulo_normalizado`,
  `local_normalizado`, `data_ref` e `chave_dedup` na mesma transação.
- Colisão de chave de deduplicação aborta a correção (nada é gravado).
- Evento `duplicado` é registro histórico e não pode ser corrigido.
- Evento já publicado pode ser corrigido: a consulta passa a mostrar o valor
  novo, mas **nada é reenviado ao canal** — o comando avisa isso.
- Antes/depois vão para `auditoria` com ação `event.correct`.

### `/atualizar`

Não coleta e não publica. Mostra, por fonte canônica, a última sessão de coleta
e o status, além dos pendentes de revisão. A coleta continua sendo
`python -m collectors.cli` / cron; o comando existe para responder "a base está
velha?" sem dar a um operador de chat o poder de disparar rede.

## Limites conhecidos

- `/semana`, `/gratis` etc. cortam o que já começou; um evento de hoje às 10h
  não aparece às 15h.
- Listagens têm teto (15–20 itens na resposta; 300–500 candidatos no filtro em
  Python). Não há paginação por botão.
- Recortes por palavra-chave herdam o vocabulário da fonte; `categoria` vazia
  nos coletores é a causa raiz da imprecisão.
- `/fontes` reflete o catálogo **deste banco**. Banco sem coleta não tem
  `fontes` e o comando diz isso.
- Nenhum comando novo publica, altera collectors/sources ou acessa a rede.

## Verificação

```bash
.venv/bin/python -m pytest tests/test_comandos_publicos.py -q
```

19 testes cobrem janelas, gratuidade, recortes, noite, UFBA, bairro, fontes,
correção com auditoria, duplicatas, negação de permissão e ausência de envios
(`transport.events == []`).
