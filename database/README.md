# Núcleo de dados: agenda cultural de Salvador

SQLite puro (stdlib), sem ORM e sem dependências externas. Tudo em UTC;
datas sem fuso na entrada são lidas como America/Bahia.

## Arquivos

| Arquivo | Papel |
| --- | --- |
| `schema.sql` | DDL: fontes, sessoes, eventos, proveniencia, auditoria, índices e triggers |
| `connection.py` | `conectar`, `inicializar` (idempotente), `transacao`, `abrir_banco` |
| `models.py` | `Fonte`, `Sessao`, `Evento`, `Proveniencia` e conversão de datas |
| `validation.py` | Validação conservadora de Salvador, decide pendente x rejeitado |
| `dedup.py` | Normalização, chave exata SHA-256, similaridade de títulos |
| `repository.py` | Ingestão transacional, aprovação/rejeição, consultas, auditoria |
| `erros.py` | `ErroBanco`, `DadoInvalido`, `TransicaoInvalida` |

## Fluxo de ingestão

```
coletor -> iniciar_sessao -> ingerir_evento(evento, proveniencia) -> status pendente
                                                                    -> revisão humana
                                                                       aprovar_evento | rejeitar_evento
```

1. `ingerir_evento` exige sessão aberta da mesma fonte e `hash_conteudo`
   sha256 (64 hex) do material coletado.
2. A validação nunca aprova: o evento aceito nasce `pendente`.
3. Rejeições são gravadas em `eventos` com `status='rejeitado'` quando a
   data é persistível; quando a data é inválida, a recusa fica em
   `auditoria` (`acao='ingestao_rejeitada'`).
4. Duplicata exata (mesma chave) não cria evento novo: grava proveniência
   no canônico, auditoria `duplicado` e incrementa o contador da sessão.
5. Duplicata aproximada (mesmo dia local, título >= 0.90) não é fundida:
   entra `pendente` com motivo `possivel_duplicado:<id>`.

## Regras de Salvador (conservadoras)

Rejeitam: município explícito diferente de Salvador, UF diferente de BA,
coordenadas fora da caixa `lat [-13.10, -12.75] / lon [-38.62, -38.30]`,
coordenadas incompletas, município ausente e sem coordenadas na caixa,
data inválida, fim antes do início, início a mais de 730 dias, evento
encerrado há mais de 1 dia, URL de ingresso não http(s).

Ficam pendentes com aviso: UF ausente, município ausente mas coordenadas
dentro da caixa (`municipio_inferido_por_coordenadas`).

Aprovação exige ator e só ocorre a partir de `pendente`. Toda transição
gera linha em `auditoria`, que é append-only (triggers bloqueiam UPDATE e
DELETE).

## API

```python
conn = abrir_banco("agenda.db")          # ou ":memory:"
fonte_id = registrar_fonte(conn, Fonte(nome="...", url_base="https://..."))
sessao_id = iniciar_sessao(conn, fonte_id)

resultado = ingerir_evento(conn, Evento(...), Proveniencia(...))
# ResultadoIngestao(status, evento_id, motivos, chave_dedup, duplicata_de)

listar_pendentes(conn, limite=100)
obter_evento(conn, evento_id)            # inclui proveniencia
aprovar_evento(conn, evento_id, ator="curador", motivo="checado no site")
rejeitar_evento(conn, evento_id, ator="curador", motivo="data errada")
finalizar_sessao(conn, sessao_id, status="concluida")
listar_auditoria(conn, entidade="evento", entidade_id=evento_id)
```

## Testes

```bash
pytest tests/test_database.py -q
```

`tests/test_database.py` cobre schema, sessões, validação de Salvador,
deduplicação exata e aproximada, proveniência, transições e imutabilidade
da auditoria.
