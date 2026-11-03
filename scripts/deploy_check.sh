#!/usr/bin/env bash
# Verificação de implantação (não implanta nada).
#
#   scripts/deploy_check.sh                 # tudo que for possível localmente
#   scripts/deploy_check.sh --skip-compose   # sem docker compose
#
# Etapas: compilação, preflight, suíte do projeto, testes de implantação,
# validação do compose, build da imagem (só se o daemon estiver acessível).
# Sai com 0 só se todas as etapas obrigatórias passarem.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

PY="python3"
if [ -x .venv/bin/python ]; then PY=".venv/bin/python"; fi

SKIP_COMPOSE=0
SKIP_BUILD=0
for arg in "$@"; do
  case "$arg" in
    --skip-compose) SKIP_COMPOSE=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    *) echo "argumento desconhecido: $arg" >&2; exit 2 ;;
  esac
done

fail=0
step() {
  local name="$1"; shift
  local out code
  out="$("$@" 2>&1)"; code=$?
  if [ $code -eq 0 ]; then
    printf 'PASS  %s\n' "$name"
  else
    printf 'FAIL  %s (exit %s)\n' "$name" "$code"
    printf '%s\n' "$out" | sed 's/^/      /' | tail -n 40
    fail=$((fail + 1))
  fi
}

skip() { printf 'SKIP  %s (%s)\n' "$1" "$2"; }

echo "== verificação de implantação =="
printf 'python: %s\n' "$($PY -V 2>&1)"

step "compilação (scripts/ e bot/)" "$PY" -m compileall -q scripts bot database
step "preflight (modo dev)" "$PY" scripts/preflight.py --mode dev --scan-repo
step "suíte do projeto" "$PY" -m pytest tests -q -m "not deploy"
step "testes de implantação" "$PY" -m pytest tests/deploy -q

if [ "$SKIP_COMPOSE" -eq 1 ]; then
  skip "docker compose config" "--skip-compose"
elif command -v docker >/dev/null 2>&1; then
  # `.env` é opcional no compose: a validação roda com o que existir e, sem o
  # arquivo, resolve para o modo seguro (token vazio, execução desligada).
  if [ -f .env ]; then
    step "docker compose config (com .env)" docker compose config --quiet
  else
    step "docker compose config (sem .env, modo seguro)" docker compose config --quiet
  fi

  if [ "$SKIP_BUILD" -eq 1 ]; then
    skip "build da imagem" "--skip-build"
  elif docker info >/dev/null 2>&1; then
    step "build da imagem" docker build -t agenda-cultural-salvador:check .
  else
    skip "build da imagem" "daemon docker inacessível"
  fi
else
  skip "docker compose config" "docker ausente"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "RESULTADO: OK — nenhuma etapa obrigatória falhou."
  exit 0
fi
echo "RESULTADO: $fail etapa(s) falharam."
exit 1
