# Feiras livres e culturais de Salvador — dados, fontes e limites

Pesquisa e implementação feitas em **03/11/2026**. Tudo abaixo foi medido: a
sondagem ao vivo está em `sources/probe_report_feiras.json`, os recortes das
fontes em `sources/feiras/` e as provas campo a campo no banco (`feiras_evidencias`).

## O que existe agora

| peça | onde |
|---|---|
| catálogo com evidência por campo | `feiras/catalogo.py` |
| carga/consulta SQLite | `feiras/repository.py` |
| CLI (`carregar`, `listar`, `buscar`, `evidencias`, `fontes`, `verificar`) | `feiras/cli.py` |
| tabelas `feiras`, `feiras_fontes`, `feiras_evidencias` | `database/schema.sql` (schema v3) |
| comandos `/feiras` e `/feira SLUG` no bot | `bot/handlers.py` |
| 20 testes offline | `tests/test_feiras.py` |

Carga no banco de trabalho e resumo real:

```
$ python -m feiras.cli --db data/agenda.db carregar
catálogo carregado: 25 criada(s), 0 atualizada(s), 0 inalterada(s), 55 evidência(s) nova(s), 5 fonte(s)
$ python -m feiras.cli --db data/agenda.db resumo
{"feiras": 25, "com_dia_publicado": 23, "sem_dia_publicado": 2, "evidencias": 55, "fontes": 5}
```

## Modelo: por que dá para conferir cada linha

Cada feira tem `bairro`, `endereco`, `dias`, `horario`, `feirantes`,
`status_verificacao` e uma lista de evidências `(campo, fonte, trecho)`. O trecho
é **literal** e o teste `test_evidencia_confere_com_snapshot` exige que ele
exista, byte a byte, no recorte preservado da fonte. Sem trecho, o campo fica
vazio.

Duas colunas existem só para não inventar:

* `bairro_base` — `fonte` (a fonte declara o bairro), `nome_oficial` (o bairro é
  o próprio nome oficial da feira) ou `nao_informado` (nesse caso `bairro` é
  obrigatoriamente vazio, garantido por `CHECK` no schema);
* `status_verificacao` — `verificado` (endereço **e** dia publicados),
  `parcial` (existe, mas falta dia), `sem_dia_fixo` (a agenda publica por
  edição). `verificado` sem dia é rejeitado pelo schema.

Evidência é *append-only*: `UPDATE`/`DELETE` em `feiras_evidencias` abortam por
trigger, igual a `auditoria`.

## Fontes usadas e o que cada uma sustenta

| chave | fonte | sustenta | confiabilidade | situação medida em 03/11 |
|---|---|---|---|---|
| `semop` | SEMOP/PMS — *Solicitações e Localizações de Feiras* | nome, endereço, nº de feirantes das 23 feiras municipais | alta | **TCP filtrado** (timeout) desta rede; conteúdo lido por extração externa |
| `senac` | Observatório do Patrimônio Gastronômico do Nordeste (SENAC-BA) | **dia de funcionamento** ("Terça a domingo") | média (publicado em 2019-11-29) | respondeu 200 na 1ª leitura e caiu para timeout minutos depois |
| `visit` | Visit Salvador da Bahia (portal oficial de turismo) | bairro, endereço complementar, programação cultural, Feira Livre de Pescados | média | 200 · 160.069 bytes |
| `pelourinho` | Pelourinho Dia e Noite (SECULT/PMS) | Feira de Artesanato da Praça da Sé publicada **por edição** | alta | TCP filtrado (mesma faixa municipal) |
| `acm` | Instituto ACM (organizador da Feira da Sé) | "datas e horários serão divulgadas a cada edição" | média | 200 · 84.522 bytes |

Sondagem ao vivo (`python -m feiras.cli verificar --salvar`) reproduziu o quadro:
`semop`, `senac` e `pelourinho` = `FonteIndisponivel`; `visit` e `acm` = 200.

## Cobertura: 23 feiras com dia + 2 sem dia

As 23 feiras municipais vêm da tabela oficial da SEMOP (endereço e feirantes) e
têm dia `terça a domingo` pela tabela do SENAC-BA. Não há, hoje, publicação
municipal com dia por feira — a SEMOP publica localização e quantidade de
feirantes, não o dia.

Fora das 23:

* **Feira Livre de Pescados / Porto das Sardinhas** (São João do Cabrito) —
  `parcial`: o portal de turismo diz "os dias de maior fluxo são de quinta a
  domingo", que é fluxo, não funcionamento. Ficou sem dia, com o trecho citado.
* **Feira de Artesanato da Praça da Sé** — `sem_dia_fixo`, `regular = 0`: cada
  edição tem data própria ("Data: 27 a 30/agosto | 9h às 19h") e o organizador
  informa que datas e horários saem a cada edição. É feira cultural, mas não é
  feira de dia fixo — entra no banco e **não** entra em `/feiras sabado`.

## O que não foi possível obter (e por quê)

1. **Dia oficial por feira.** A fonte municipal não publica. O dia usado é o do
   SENAC-BA, de **2019** — confiabilidade média, revalidação pendente. É o
   único documento encontrado que declara dia por feira; não foi inventado nem
   deduzido de "todo mundo sabe".
2. **Horário por feira.** Nenhuma fonte consultada publica horário individual
   das feiras livres (só dos mercados e das lojas). `horario` ficou vazio em
   todas as 25 linhas; o bot não mostra horário de feira.
3. **Acesso direto à SEMOP/Pelourinho desta rede.** Hosts `*.salvador.ba.gov.br`
   não completam TCP (timeout de 25 s, inclusive em `curl`). O recorte foi
   obtido por extração externa e está preservado; o coletor do projeto continua
   sem contornar bloqueio (sem proxy, sem UA alternativo, sem headless). Para
   revalidar de forma direta é preciso executar de uma rede com rota legítima
   para `177.20.x`.
4. **Mercado Iaô** (Rio Vermelho) foi pesquisado e **não entrou**: as fontes
   consultadas divulgam edições com datas específicas, sem dia fixo. Não há
   linha no banco para ele.
5. **Contagem de feirantes** é a da SEMOP; onde as fontes divergem (grafia
   "Fazendo/Fazenda", "Coustos/Coutos", endereços de Sete Portas, Vale das
   Pedrinhas, São Cristóvão) a divergência está em `observacao` e nas duas
   evidências — nada foi "corrigido" silenciosamente.

## Consulta

```
python -m feiras.cli --db data/agenda.db listar                 # todas
python -m feiras.cli --db data/agenda.db listar --dia hoje
python -m feiras.cli --db data/agenda.db listar --bairro Liberdade
python -m feiras.cli --db data/agenda.db evidencias sao-joaquim  # provas
python -m feiras.cli --db data/agenda.db verificar --salvar      # sonda ao vivo
```

No bot (público, sem permissão especial, leitura pura):

```
/feiras                 -> feiras de hoje (America/Bahia)
/feiras sabado          -> por dia
/feiras bairro Liberdade
/feiras pelourinho      -> busca por nome/bairro/endereço
/feira sao-joaquim      -> ficha + trecho e URL da fonte
```

`/feiras segunda` responde "nenhuma feira com dia de funcionamento publicado" —
segunda é o dia sem feira na fonte, e o bot diz isso em vez de inventar.

## Limitações assumidas

* `/feiras` não cria evento, não aprova e não publica nada: as tabelas de feiras
  são núcleo separado de `eventos`.
* O bot só lê. Carga/atualização é operação de CLI (`feiras.cli carregar`),
  idempotente e não destrutiva — recarregar não apaga linha inserida à mão
  (teste `test_recarregar_preserva_linha_extra`).
* `hash_snapshot` guarda o SHA-256 do recorte; se a fonte mudar, `verificar`
  mostra o estado de rede mas **não** atualiza o snapshot automaticamente.
  Atualizar prova é decisão humana: substituir o arquivo em `sources/feiras/`,
  rodar `carregar` e conferir `evidencias`.
