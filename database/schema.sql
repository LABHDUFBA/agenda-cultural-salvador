-- Núcleo de dados da agenda cultural de Salvador.
-- Todas as datas são ISO 8601 em UTC (sufixo +00:00).
-- A versão do schema fica em PRAGMA user_version (ver database/connection.py).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS fontes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nome           TEXT    NOT NULL UNIQUE,
    url_base       TEXT    NOT NULL,
    tipo           TEXT    NOT NULL DEFAULT 'site',
    confiabilidade TEXT    NOT NULL DEFAULT 'media'
                           CHECK (confiabilidade IN ('alta', 'media', 'baixa')),
    criado_em      TEXT    NOT NULL,
    CHECK (length(trim(nome)) > 0),
    CHECK (url_base LIKE 'http://%' OR url_base LIKE 'https://%')
);

CREATE TABLE IF NOT EXISTS sessoes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fonte_id       INTEGER NOT NULL REFERENCES fontes(id) ON DELETE RESTRICT,
    iniciada_em    TEXT    NOT NULL,
    finalizada_em  TEXT,
    status         TEXT    NOT NULL DEFAULT 'aberta'
                           CHECK (status IN ('aberta', 'concluida', 'falha')),
    coletados      INTEGER NOT NULL DEFAULT 0 CHECK (coletados >= 0),
    aceitos        INTEGER NOT NULL DEFAULT 0 CHECK (aceitos >= 0),
    rejeitados     INTEGER NOT NULL DEFAULT 0 CHECK (rejeitados >= 0),
    duplicados     INTEGER NOT NULL DEFAULT 0 CHECK (duplicados >= 0),
    erro           TEXT,
    CHECK (finalizada_em IS NULL OR finalizada_em >= iniciada_em),
    CHECK (status = 'aberta' OR finalizada_em IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS eventos (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo             TEXT    NOT NULL,
    titulo_normalizado TEXT    NOT NULL,
    descricao          TEXT    NOT NULL DEFAULT '',
    inicio_em          TEXT    NOT NULL,
    fim_em             TEXT,
    data_ref           TEXT    NOT NULL,
    local_nome         TEXT    NOT NULL DEFAULT '',
    local_normalizado  TEXT    NOT NULL DEFAULT '',
    endereco           TEXT    NOT NULL DEFAULT '',
    bairro             TEXT    NOT NULL DEFAULT '',
    municipio          TEXT    NOT NULL DEFAULT '',
    uf                 TEXT    NOT NULL DEFAULT '',
    latitude           REAL,
    longitude          REAL,
    categoria          TEXT    NOT NULL DEFAULT '',
    url_ingresso       TEXT    NOT NULL DEFAULT '',
    preco              TEXT    NOT NULL DEFAULT '',
    status             TEXT    NOT NULL DEFAULT 'pendente'
                               CHECK (status IN ('pendente', 'aprovado', 'rejeitado', 'duplicado')),
    motivo             TEXT    NOT NULL DEFAULT '',
    chave_dedup        TEXT    UNIQUE,
    evento_canonico_id INTEGER REFERENCES eventos(id) ON DELETE SET NULL,
    criado_em          TEXT    NOT NULL,
    atualizado_em      TEXT    NOT NULL,
    -- Integração com o bot/Telegram: submissão humana e autorização do
    -- organizador. A publicação NÃO é um status editorial (ver
    -- tentativas_publicacao): aprovar e publicar são fatos distintos.
    url_fonte              TEXT    NOT NULL DEFAULT '',
    enviado_por            INTEGER NOT NULL DEFAULT 0,
    autorizacao_confirmada INTEGER NOT NULL DEFAULT 0
                           CHECK (autorizacao_confirmada IN (0, 1)),
    autorizacao_referencia TEXT    NOT NULL DEFAULT '',
    CHECK (length(trim(titulo)) > 0),
    CHECK (status <> 'duplicado' OR evento_canonico_id IS NOT NULL),
    CHECK (fim_em IS NULL OR fim_em >= inicio_em),
    CHECK ((latitude IS NULL AND longitude IS NULL)
           OR (latitude IS NOT NULL AND longitude IS NOT NULL)),
    CHECK (latitude IS NULL OR (latitude BETWEEN -90.0 AND 90.0)),
    CHECK (longitude IS NULL OR (longitude BETWEEN -180.0 AND 180.0))
);

CREATE INDEX IF NOT EXISTS idx_eventos_status      ON eventos(status);
CREATE INDEX IF NOT EXISTS idx_eventos_data        ON eventos(data_ref, status);
CREATE INDEX IF NOT EXISTS idx_eventos_fuzzy       ON eventos(data_ref, local_normalizado);

CREATE TABLE IF NOT EXISTS proveniencia (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id     INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
    fonte_id      INTEGER NOT NULL REFERENCES fontes(id) ON DELETE RESTRICT,
    sessao_id     INTEGER REFERENCES sessoes(id) ON DELETE SET NULL,
    url_original  TEXT    NOT NULL,
    capturado_em  TEXT    NOT NULL,
    hash_conteudo TEXT    NOT NULL CHECK (length(hash_conteudo) = 64),
    trecho        TEXT    NOT NULL DEFAULT '',
    CHECK (url_original LIKE 'http://%' OR url_original LIKE 'https://%'),
    UNIQUE (evento_id, url_original, hash_conteudo)
);

CREATE INDEX IF NOT EXISTS idx_proveniencia_evento ON proveniencia(evento_id);

-- Trilha de auditoria: apenas inserção. UPDATE e DELETE são bloqueados por trigger.
CREATE TABLE IF NOT EXISTS auditoria (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entidade        TEXT    NOT NULL,
    entidade_id     INTEGER,
    acao            TEXT    NOT NULL,
    estado_anterior TEXT,
    estado_novo     TEXT,
    ator            TEXT    NOT NULL DEFAULT 'sistema',
    detalhe         TEXT    NOT NULL DEFAULT '',
    ocorrido_em     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auditoria_entidade ON auditoria(entidade, entidade_id);

CREATE TRIGGER IF NOT EXISTS auditoria_somente_insercao_update
BEFORE UPDATE ON auditoria
BEGIN
    SELECT RAISE(ABORT, 'auditoria e append-only: UPDATE nao permitido');
END;

CREATE TRIGGER IF NOT EXISTS auditoria_somente_insercao_delete
BEFORE DELETE ON auditoria
BEGIN
    SELECT RAISE(ABORT, 'auditoria e append-only: DELETE nao permitido');
END;

-- ---------------------------------------------------------------------------
-- Tabelas operacionais do bot/Telegram. Ficam no MESMO banco e no MESMO
-- schema canônico: identidade de operador e tentativas de publicação são
-- fatos da operação, não um segundo núcleo de eventos.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS usuarios (
    telegram_id   INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL DEFAULT '',
    papel         TEXT    NOT NULL DEFAULT 'public'
                          CHECK (papel IN ('public', 'editor', 'admin')),
    criado_em     TEXT    NOT NULL,
    atualizado_em TEXT    NOT NULL
);

-- Uma linha por tentativa de publicação (inclui dry-run, bloqueio e falha).
-- "Publicado" é derivado daqui: resultado='sent' e modo='execute'. Isso evita
-- um segundo campo de estado editorial em eventos.
CREATE TABLE IF NOT EXISTS tentativas_publicacao (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id  INTEGER NOT NULL REFERENCES eventos(id) ON DELETE RESTRICT,
    modo       TEXT    NOT NULL CHECK (modo IN ('dry_run', 'execute')),
    resultado  TEXT    NOT NULL
                       CHECK (resultado IN ('dry_run', 'sent', 'skipped', 'blocked', 'failed')),
    detalhe    TEXT    NOT NULL DEFAULT '',
    referencia TEXT    NOT NULL DEFAULT '',
    criado_em  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tentativas_evento ON tentativas_publicacao(evento_id);
CREATE INDEX IF NOT EXISTS idx_tentativas_envio  ON tentativas_publicacao(modo, resultado, criado_em);

-- ---------------------------------------------------------------------------
-- Feiras livres e culturais de Salvador.
--
-- Núcleo separado de `eventos` de propósito: feira é lugar recorrente (bairro,
-- dia de funcionamento), não acontecimento datado. Nada aqui vira evento nem
-- é publicado automaticamente.
--
-- `bairro_base` e `status_verificacao` existem para não inventar: dizem se o
-- bairro veio da fonte, do nome oficial da feira ou não foi informado, e se o
-- dia de funcionamento está publicado. `feiras_evidencias` guarda o trecho
-- literal que sustenta cada campo.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS feiras_fontes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chave          TEXT    NOT NULL UNIQUE,
    nome           TEXT    NOT NULL,
    organizacao    TEXT    NOT NULL DEFAULT '',
    url            TEXT    NOT NULL,
    confiabilidade TEXT    NOT NULL CHECK (confiabilidade IN ('alta', 'media', 'baixa')),
    acessibilidade TEXT    NOT NULL
                           CHECK (acessibilidade IN ('ok', 'bloqueada', 'indisponivel',
                                                     'nao_suportada', 'nao_verificada')),
    http_status    INTEGER,
    metodo_captura TEXT    NOT NULL
                           CHECK (metodo_captura IN ('http_direto', 'extracao_externa',
                                                     'registro_arquivado')),
    snapshot_path  TEXT    NOT NULL DEFAULT '',
    hash_snapshot  TEXT    NOT NULL DEFAULT ''
                           CHECK (hash_snapshot = '' OR length(hash_snapshot) = 64),
    observacao     TEXT    NOT NULL DEFAULT '',
    verificado_em  TEXT    NOT NULL,
    CHECK (length(trim(nome)) > 0),
    CHECK (url LIKE 'http://%' OR url LIKE 'https://%')
);

CREATE TABLE IF NOT EXISTS feiras (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    slug               TEXT    NOT NULL UNIQUE,
    nome               TEXT    NOT NULL,
    tipo               TEXT    NOT NULL
                               CHECK (tipo IN ('livre', 'cultural', 'artesanato', 'misto')),
    bairro             TEXT    NOT NULL DEFAULT '',
    bairro_base        TEXT    NOT NULL DEFAULT 'nao_informado'
                               CHECK (bairro_base IN ('fonte', 'nome_oficial', 'nao_informado')),
    endereco           TEXT    NOT NULL DEFAULT '',
    -- dias em ordem canônica, separados por vírgula: "terca,quarta,..."
    dias               TEXT    NOT NULL DEFAULT '',
    horario            TEXT    NOT NULL DEFAULT '',
    feirantes          INTEGER CHECK (feirantes IS NULL OR feirantes >= 0),
    regular            INTEGER NOT NULL DEFAULT 1 CHECK (regular IN (0, 1)),
    status_verificacao TEXT    NOT NULL
                               CHECK (status_verificacao IN ('verificado', 'parcial',
                                                             'sem_dia_fixo', 'nao_verificado')),
    observacao         TEXT    NOT NULL DEFAULT '',
    criado_em          TEXT    NOT NULL,
    atualizado_em      TEXT    NOT NULL,
    CHECK (length(trim(nome)) > 0),
    -- 'verificado' exige dia publicado: sem dia, o status é 'parcial'.
    CHECK (status_verificacao <> 'verificado' OR dias <> ''),
    CHECK (bairro_base <> 'nao_informado' OR bairro = '')
);

CREATE INDEX IF NOT EXISTS idx_feiras_bairro ON feiras(bairro);
CREATE INDEX IF NOT EXISTS idx_feiras_tipo   ON feiras(tipo, status_verificacao);

-- Evidência é append-only: sem UPDATE, sem DELETE (mesma regra de `auditoria`).
CREATE TABLE IF NOT EXISTS feiras_evidencias (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    feira_id      INTEGER NOT NULL REFERENCES feiras(id) ON DELETE CASCADE,
    fonte_id      INTEGER NOT NULL REFERENCES feiras_fontes(id) ON DELETE RESTRICT,
    campo         TEXT    NOT NULL,
    trecho        TEXT    NOT NULL,
    capturado_em  TEXT    NOT NULL,
    CHECK (length(trim(campo)) > 0),
    CHECK (length(trim(trecho)) > 0),
    UNIQUE (feira_id, fonte_id, campo, trecho)
);

CREATE INDEX IF NOT EXISTS idx_feiras_evidencias_feira ON feiras_evidencias(feira_id);

CREATE TRIGGER IF NOT EXISTS feiras_evidencias_append_only_update
BEFORE UPDATE ON feiras_evidencias
BEGIN
    SELECT RAISE(ABORT, 'feiras_evidencias e append-only: UPDATE nao permitido');
END;

CREATE TRIGGER IF NOT EXISTS feiras_evidencias_append_only_delete
BEFORE DELETE ON feiras_evidencias
BEGIN
    SELECT RAISE(ABORT, 'feiras_evidencias e append-only: DELETE nao permitido');
END;
