# syntax=docker/dockerfile:1
#
# Agenda Cultural Salvador — imagem de execução.
#
# O runtime do projeto é 100% biblioteca padrão; a imagem não instala
# dependências além de tzdata (fusos) e ca-certificates (HTTPS para a
# Bot API).  Nenhuma credencial é embutida: tudo vem por ambiente
# (ver .env.example).
#
# Build:  docker build -t agenda-cultural-salvador:local .
# Uso:    docker run --env-file .env -v agenda-data:/app/data agenda-cultural-salvador:local

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/Bahia \
    LANG=C.UTF-8 \
    AGENDA_DB=/app/data/agenda.db \
    AGENDA_BACKUP_DIR=/app/backups

# tzdata: America/Bahia não é um link fixo, precisa da base IANA.
# ca-certificates: TLS da api.telegram.org.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tzdata \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

WORKDIR /app

# Camada de dependências separada do código: rebuild de código não reinstala.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Usuário sem privilégios e sem shell de login.
RUN groupadd --system --gid 10001 agenda \
    && useradd --system --uid 10001 --gid agenda --home-dir /app --shell /usr/sbin/nologin agenda

COPY --chown=10001:10001 . .

# Volumes de dados e backups: o dono precisa existir na imagem para que o
# volume nomeado herde a permissão correta no primeiro uso.
RUN mkdir -p /app/data /app/backups \
    && chown -R agenda:agenda /app/data /app/backups \
    && chmod 750 /app/data /app/backups

USER 10001:10001

# Long-polling: o processo não abre porta, só faz saída HTTPS.
# Healthcheck não exige token e não toca a rede.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "scripts/healthcheck.py", "--quiet"]

# Padrão: bot em long-polling.  O scheduler e o CLI administrativo são
# acionados sobrescrevendo o comando (ver docker-compose.yml).
CMD ["python", "-m", "bot.app"]
