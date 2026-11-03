#!/usr/bin/env python3
"""Agendador de tarefas de manutenção (biblioteca padrão, fuso America/Bahia).

Não é um cron genérico: executa apenas as tarefas registradas abaixo, cada
uma com comandos fixos (sem shell, sem interpolação), o que elimina a
superfície de injeção de um crontab editável.

O que ele agenda — e o que ele NÃO faz:
  * agenda backup, checagem de integridade, relatório de pendentes e a rotina
    de coleta (06, 12, 17 e 22, única tarefa com rede; grava sempre como
    pendente — nunca aprova);
  * NÃO publica eventos automaticamente: a tarefa "publish" pode ser
    DECLARADA na agenda (janela editorial, desligada) mas nunca é executada
    por este processo — publicação continua sendo comando humano com frase de
    confirmação (ver bot/publisher.py e docs/security.md).

Formato de config/schedule.json:
    {"timezone": "America/Bahia",
     "jobs": [{"name": "...", "task": "backup", "at": "03:15",
               "days": ["thu", "sun"],     # opcional; padrão: todos os dias
               "enabled": true, "args": {"keep": 14}}]}

Regras de execução:
  * uma vez por slot (job + dia) no horário local do fuso configurado;
  * `days` restringe a dias da semana (seg..dom, mon..sun, 1..7, "*");
  * atraso tolerado (catch-up) de até AGENDA_CATCHUP_HOURS (padrão 6 h);
    além disso o slot é registrado como perdido para não rodar um backup às
    22 h por causa de uma janela antiga;
  * trava de instância única (flock) no arquivo de estado;
  * heartbeat gravado a cada tick — o healthcheck do container usa isso;
  * SIGTERM/SIGINT encerram com código 0 entre tarefas.

Exemplos:
    python scripts/scheduler.py --print-plan
    python scripts/scheduler.py --print-plan --json
    python scripts/scheduler.py --once --dry-run
    python scripts/scheduler.py --once
    python scripts/scheduler.py            # loop de longa duração
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("agenda.scheduler")


def _env_int(name: str, default: int) -> int:
    """Lê inteiro do ambiente sem deixar valor inválido derrubar o processo."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("valor inválido em %s=%r; usando %s", name, raw, default)
        return default


DEFAULT_CONFIG = os.environ.get("AGENDA_SCHEDULE") or "config/schedule.json"
DEFAULT_STATE = os.environ.get("AGENDA_SCHEDULER_STATE") or "data/scheduler_state.json"
DEFAULT_TZ = "America/Bahia"
CATCHUP_HOURS = _env_int("AGENDA_CATCHUP_HOURS", 6)
TICK_SECONDS = _env_int("AGENDA_SCHEDULER_TICK", 30)
TASK_TIMEOUT = _env_int("AGENDA_TASK_TIMEOUT", 1800)


class ScheduleError(Exception):
    """Configuração inválida — falha antes de qualquer execução."""


# --------------------------------------------------------------------------
# Registro de tarefas: nome -> argv montado a partir dos args do job.
# Nada aqui abre socket; todas chamam scripts locais ou o CLI administrativo.
# --------------------------------------------------------------------------
def _py(script: str) -> list[str]:
    return [sys.executable, str(ROOT / "scripts" / script)]


def task_backup(args: dict) -> list[str]:
    argv = _py("backup.py") + ["create", "--keep", str(int(args.get("keep", 14)))]
    if not args.get("verify", True):
        argv.append("--no-verify")
    return argv


def task_integrity(args: dict) -> list[str]:
    return [sys.executable, str(ROOT / "scripts" / "healthcheck.py"), "--json"]


def task_pending_report(args: dict) -> list[str]:
    # Offline: lista o que está parado esperando autorização/aprovação.
    return [sys.executable, "-m", "bot.admin", "events",
            "--status", "pending_review", "--status", "approved"]


def task_coleta(args: dict) -> list[str]:
    """Coleta em lote (rede) — rotina de 06, 12, 17 e 22.

    Chama ``scripts/coleta.py``, que percorre as fontes **coletáveis** do
    catálogo (ou as de ``fontes``) e, com ``gravar``, persiste tudo como
    pendente/rejeitado. Nada é aprovado nem publicado por esta tarefa.
    """
    argv = [sys.executable, str(ROOT / "scripts" / "coleta.py"), "--json"]
    fontes = args.get("fontes") or []
    if fontes:
        argv += ["--fontes", ",".join(fontes)]
    if args.get("limite"):
        argv += ["--limite", str(int(args["limite"]))]
    if args.get("gravar"):
        argv.append("--gravar")
    return argv


TASKS: dict[str, Callable[[dict], list[str]]] = {
    "backup": task_backup,
    "integrity": task_integrity,
    "pending_report": task_pending_report,
    "coleta": task_coleta,
}

# Tarefas que a agenda pode DECLARAR (para documentar janela editorial) mas que
# este processo nunca executa.  `publish` está aqui de propósito: publicar é
# ato humano com frase de confirmação.  Um job declarado com estas tarefas
# precisa estar `"enabled": false` — a validação recusa o contrário, e
# `run_task` recusa mesmo que alguém contorne a validação em memória.
DECLARABLE_TASKS: frozenset[str] = frozenset({"publish"})

# --------------------------------------------------------------------------
# Dias da semana (ISO: 1=segunda ... 7=domingo)
# --------------------------------------------------------------------------
ALL_DAYS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)

DAY_ALIASES: dict[str, int] = {
    "1": 1, "seg": 1, "segunda": 1, "segunda-feira": 1, "mon": 1, "monday": 1,
    "2": 2, "ter": 2, "terca": 2, "terça": 2, "terca-feira": 2, "terça-feira": 2,
    "tue": 2, "tues": 2, "tuesday": 2,
    "3": 3, "qua": 3, "quarta": 3, "quarta-feira": 3, "wed": 3, "wednesday": 3,
    "4": 4, "qui": 4, "quinta": 4, "quinta-feira": 4, "thu": 4, "thur": 4,
    "thurs": 4, "thursday": 4,
    "5": 5, "sex": 5, "sexta": 5, "sexta-feira": 5, "fri": 5, "friday": 5,
    "6": 6, "sab": 6, "sáb": 6, "sabado": 6, "sábado": 6, "sat": 6, "saturday": 6,
    "7": 7, "dom": 7, "domingo": 7, "sun": 7, "sunday": 7,
}
ALL_DAYS_ALIASES = {"*", "todos", "todas", "diario", "diário", "diariamente",
                    "daily", "everyday", "every-day", "all"}


def parse_days(value) -> tuple[int, ...]:
    """Normaliza `days` para uma tupla ordenada de dias ISO.

    Aceita ausência (todos os dias), a string "*" e listas/strings com nomes
    em português ou inglês ("qui,dom", ["thu", "sun"], [4, 7]).
    """
    if value is None:
        return ALL_DAYS
    if isinstance(value, str):
        text = value.strip().lower()
        if not text or text in ALL_DAYS_ALIASES:
            return ALL_DAYS
        tokens = [t for t in re.split(r"[,;\s]+", text) if t]
    elif isinstance(value, (list, tuple)):
        tokens = []
        for item in value:
            if isinstance(item, bool):
                raise ScheduleError(f"dia inválido: {item!r}")
            if isinstance(item, int):
                tokens.append(str(item))
            elif isinstance(item, str):
                token = item.strip().lower()
                if token in ALL_DAYS_ALIASES:
                    return ALL_DAYS
                tokens.append(token)
            else:
                raise ScheduleError(f"dia inválido: {item!r}")
    else:
        raise ScheduleError(f"'days' precisa ser string ou lista, não {type(value).__name__}")

    if not tokens:
        raise ScheduleError("'days' vazio — use \"*\" para todos os dias")

    days: set[int] = set()
    for token in tokens:
        day = DAY_ALIASES.get(token)
        if day is None:
            raise ScheduleError(
                f"dia desconhecido: {token!r} (use seg..dom, mon..sun ou 1..7)"
            )
        days.add(day)
    return tuple(sorted(days))


def _int_arg(args: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    raw = args.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"'{key}' precisa ser inteiro, recebido {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ScheduleError(f"'{key}' fora de faixa ({minimum}..{maximum}): {value}")
    return value


def validate_args(task: str, raw_args) -> dict:
    """Valida `args` na leitura da agenda: erro de config falha antes de rodar."""
    if raw_args is None:
        args: dict = {}
    elif isinstance(raw_args, dict):
        args = dict(raw_args)
    else:
        raise ScheduleError("'args' precisa ser objeto")

    if task == "backup":
        unknown = set(args) - {"keep", "verify"}
        if unknown:
            raise ScheduleError(f"args desconhecidos para backup: {sorted(unknown)}")
        args["keep"] = _int_arg(args, "keep", 14, 1, 365)
        verify = args.get("verify", True)
        if not isinstance(verify, bool):
            raise ScheduleError("'verify' precisa ser booleano (true/false)")
        args["verify"] = verify
    elif task == "coleta":
        unknown = set(args) - {"fontes", "limite", "gravar"}
        if unknown:
            raise ScheduleError(f"args desconhecidos para coleta: {sorted(unknown)}")
        fontes_raw = args.get("fontes")
        if fontes_raw is None:
            fontes: list[str] = []
        elif isinstance(fontes_raw, str):
            fontes = [p.strip().lower() for p in fontes_raw.split(",") if p.strip()]
        elif isinstance(fontes_raw, list):
            fontes = [str(p).strip().lower() for p in fontes_raw if str(p).strip()]
        else:
            raise ScheduleError("'fontes' precisa ser lista de ids do catálogo")
        for fonte in fontes:
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", fonte):
                raise ScheduleError(
                    f"tarefa 'coleta' exige fontes em [a-z0-9_-] (recebido {fonte!r})"
                )
        if fontes:
            # Falha na leitura, não em produção: id inexistente é erro de config.
            from sources.catalog import por_id  # noqa: PLC0415

            for fonte in fontes:
                try:
                    por_id(fonte)
                except KeyError as exc:
                    raise ScheduleError(f"fonte desconhecida no catálogo: {fonte!r}") from exc
        args["fontes"] = sorted(set(fontes))
        if "limite" in args:
            args["limite"] = _int_arg(args, "limite", 50, 1, 200)
        gravar = args.get("gravar", False)
        if not isinstance(gravar, bool):
            raise ScheduleError("'gravar' precisa ser booleano (true/false)")
        args["gravar"] = gravar
    elif task == "pending_report":
        # `bot.admin events` não aceita --limit hoje: limite é aplicado no log
        # pelo próprio agendador.  Nenhuma outra chave é aceita.
        unknown = set(args) - {"limit"}
        if unknown:
            raise ScheduleError(f"args desconhecidos para pending_report: {sorted(unknown)}")
        if "limit" in args:
            args["limit"] = _int_arg(args, "limit", 20, 1, 500)
    elif task in DECLARABLE_TASKS:
        if args:
            raise ScheduleError(f"tarefa '{task}' não aceita args")
    return args


# --------------------------------------------------------------------------
# Configuração e estado
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Job:
    name: str
    task: str
    at: tuple[int, int]
    enabled: bool = True
    args: dict = field(default_factory=dict)
    days: tuple[int, ...] = ALL_DAYS

    @property
    def slot(self) -> str:
        return f"{self.at[0]:02d}:{self.at[1]:02d}"

    @property
    def days_label(self) -> str:
        if self.days == ALL_DAYS:
            return "todos"
        names = {1: "seg", 2: "ter", 3: "qua", 4: "qui", 5: "sex", 6: "sab", 7: "dom"}
        return ",".join(names[d] for d in self.days)


@dataclass(frozen=True)
class Schedule:
    timezone: str
    jobs: tuple[Job, ...]

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def parse_at(value: str) -> tuple[int, int]:
    try:
        hh, mm = str(value).strip().split(":")
        hh_i, mm_i = int(hh), int(mm)
    except (ValueError, AttributeError) as exc:
        raise ScheduleError(f"horário inválido: {value!r} (use HH:MM)") from exc
    if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
        raise ScheduleError(f"horário fora de faixa: {value!r}")
    return hh_i, mm_i


def load_schedule(path: Optional[str] = None, raw: Optional[dict] = None) -> Schedule:
    if raw is None:
        target = _resolve(path or DEFAULT_CONFIG)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ScheduleError(f"agenda não encontrada: {target}") from exc
        except json.JSONDecodeError as exc:
            raise ScheduleError(f"agenda inválida ({target}): {exc}") from exc

    tz_name = raw.get("timezone") or DEFAULT_TZ
    try:
        ZoneInfo(tz_name)
    except Exception as exc:  # noqa: BLE001 - qualquer erro de fuso é fatal
        raise ScheduleError(f"fuso desconhecido: {tz_name}") from exc

    jobs_raw = raw.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise ScheduleError("agenda sem 'jobs'")

    jobs: list[Job] = []
    seen: set[str] = set()
    slots: set[tuple[str, tuple[int, int], tuple[int, ...]]] = set()
    for item in jobs_raw:
        if not isinstance(item, dict):
            raise ScheduleError("job precisa ser objeto")
        name = str(item.get("name") or "").strip()
        task = str(item.get("task") or "").strip()
        if not name or not task:
            raise ScheduleError("job sem 'name' ou 'task'")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise ScheduleError(f"nome de job inválido: {name!r} (use [A-Za-z0-9._-])")
        if name in seen:
            raise ScheduleError(f"job duplicado: {name}")
        if task not in TASKS and task not in DECLARABLE_TASKS:
            raise ScheduleError(
                f"tarefa desconhecida: {task!r} "
                f"(executáveis: {sorted(TASKS)}; declaráveis: {sorted(DECLARABLE_TASKS)})"
            )
        seen.add(name)

        at = parse_at(item.get("at", ""))
        days = parse_days(item.get("days"))
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ScheduleError(f"job {name}: 'enabled' precisa ser booleano")

        # Publicação é ato humano: a agenda pode descrever a janela, nunca
        # habilitá-la.  Bloquear na leitura evita que um "enabled": true
        # esquecido vire envio automático.
        if task in DECLARABLE_TASKS and enabled:
            raise ScheduleError(
                f"job {name}: tarefa {task!r} não pode ser habilitada — "
                "publicação exige comando humano (ver docs/security.md)"
            )

        key = (task, at, days)
        if key in slots:
            raise ScheduleError(
                f"job {name}: duplica slot {task} {at[0]:02d}:{at[1]:02d} "
                f"em {days or 'todos'}"
            )
        slots.add(key)

        jobs.append(Job(
            name=name,
            task=task,
            at=at,
            enabled=enabled,
            args=validate_args(task, item.get("args")),
            days=days,
        ))
    return Schedule(timezone=tz_name, jobs=tuple(jobs))


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    # Estado não guarda segredo, mas restringir a leitura custa nada e evita
    # expor nomes de jobs/horários a outros usuários do host.
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def file_digest(path: Optional[Path]) -> Optional[str]:
    """SHA-256 do arquivo de agenda — usado para detectar desvio em produção."""
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def write_heartbeat(path: Path, state: dict, last_status: Optional[int],
                    next_runs: dict, meta: Optional[dict] = None) -> None:
    payload = dict(state)
    payload.update({
        "heartbeat_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "last_status": last_status,
        "next_runs": next_runs,
    })
    if meta:
        payload.update(meta)
    save_state(path, payload)


# --------------------------------------------------------------------------
# Lógica de disparo (pura, testável sem relógio real)
# --------------------------------------------------------------------------
def slot_datetime(job: Job, day: datetime, tz: ZoneInfo) -> datetime:
    local = day.astimezone(tz)
    return local.replace(hour=job.at[0], minute=job.at[1], second=0, microsecond=0)


def last_run_date(job: Job, state: dict) -> Optional[str]:
    entry = (state.get("jobs") or {}).get(job.name) or {}
    return entry.get("date")


def due_slot(job: Job, now: datetime, state: dict, tz: ZoneInfo,
             catchup_hours: float = CATCHUP_HOURS) -> Optional[datetime]:
    """Slot que deve rodar agora (ou `None`).

    Um job roda no máximo uma vez por slot — a chave é a data LOCAL DO SLOT,
    não a data corrente.  Assim, um slot de quinta 22 h recuperado na sexta
    02 h continua sendo "quinta" e não impede o slot de sexta.
    """
    if not job.enabled:
        return None
    local = now.astimezone(tz)
    candidates: list[datetime] = []
    for back in (0, 1):
        day = local - timedelta(days=back)
        if day.isoweekday() not in job.days:
            continue
        slot = slot_datetime(job, day, tz)
        if local < slot:
            continue
        if local - slot > timedelta(hours=catchup_hours):
            continue
        if last_run_date(job, state) == slot.date().isoformat():
            continue
        candidates.append(slot)
    return min(candidates) if candidates else None


def is_due(job: Job, now: datetime, state: dict, tz: ZoneInfo,
           catchup_hours: float = CATCHUP_HOURS) -> bool:
    """Devia rodar agora?  Uma execução por slot, dentro da janela de catch-up.

    `now` pode estar em qualquer fuso; a comparação é feita no fuso da agenda.
    """
    return due_slot(job, now, state, tz, catchup_hours) is not None


def expired_slot(job: Job, now: datetime, state: dict, tz: ZoneInfo,
                 catchup_hours: float = CATCHUP_HOURS) -> Optional[datetime]:
    """Slot de hoje que passou da janela sem execução (para registrar perda)."""
    if not job.enabled:
        return None
    local = now.astimezone(tz)
    if local.isoweekday() not in job.days:
        return None
    slot = slot_datetime(job, local, tz)
    if local - slot <= timedelta(hours=catchup_hours):
        return None
    if last_run_date(job, state) == slot.date().isoformat():
        return None
    return slot


def next_occurrence(job: Job, now: datetime, tz: ZoneInfo,
                    catchup_hours: float = CATCHUP_HOURS) -> datetime:
    """Próxima execução: slot de hoje ainda na janela, senão o próximo dia válido."""
    local = now.astimezone(tz)
    for back in range(0, 9):
        day = local - timedelta(days=back)
        if day.isoweekday() not in job.days:
            continue
        slot = slot_datetime(job, day, tz)
        if slot + timedelta(hours=catchup_hours) >= local:
            return slot
    for ahead in range(1, 9):
        day = local + timedelta(days=ahead)
        if day.isoweekday() in job.days:
            return slot_datetime(job, day, tz)
    return slot_datetime(job, local + timedelta(days=1), tz)  # pragma: no cover


def next_run(job: Job, now: datetime, state: dict, tz: ZoneInfo,
             catchup_hours: float = CATCHUP_HOURS) -> datetime:
    """Próximo disparo considerando o que já rodou (para o heartbeat).

    Sem isso o heartbeat repetiria o slot de hoje já executado, o que parece
    atraso para quem lê o `next_runs`.
    """
    candidate = next_occurrence(job, now, tz, catchup_hours)
    if last_run_date(job, state) == candidate.date().isoformat():
        # Avança para além da janela de catch-up do slot já executado.
        candidate = next_occurrence(
            job, candidate + timedelta(hours=catchup_hours, minutes=1), tz, catchup_hours
        )
    return candidate


def run_task(job: Job, dry_run: bool) -> tuple[int, str]:
    # Segunda barreira (a primeira é a validação da agenda): nenhuma tarefa
    # declarável é executável, nem em dry-run, nem com --once.
    if job.task in DECLARABLE_TASKS:
        log.error("%s: tarefa %r é apenas declaração de janela — não executada",
                  job.name, job.task)
        return 4, f"tarefa {job.task!r} não é executável pelo agendador"

    builder = TASKS[job.task]
    try:
        argv = builder(job.args)
    except (TypeError, ValueError) as exc:
        return 2, f"args inválidos: {exc}"
    if dry_run:
        # O plano do ensaio vai para stdout: é o artefato que o operador lê
        # para conferir o que rodaria. Não pode depender de AGENDA_LOG_LEVEL,
        # senão um log em WARNING esconde exatamente o que o dry-run existe
        # para mostrar.
        print(f"[dry-run] {job.name} -> {' '.join(argv)}", flush=True)
        log.info("[dry-run] %s -> %s", job.name, " ".join(argv))
        return 0, "dry_run"
    log.info("executando %s: %s", job.name, " ".join(argv))
    try:
        proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                              timeout=TASK_TIMEOUT, env=os.environ.copy())
    except subprocess.TimeoutExpired:
        return 124, f"timeout após {TASK_TIMEOUT}s"
    except OSError as exc:
        return 2, f"não foi possível executar: {exc}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if job.task == "pending_report" and out:
        # `bot.admin events` pagina em 100; o limite da agenda corta o log.
        max_lines = int(job.args.get("limit", 0) or 0)
        if max_lines:
            out = "\n".join(out.splitlines()[:max_lines])
    if out:
        log.info("%s stdout: %s", job.name, out.replace("\n", " ")[:800])
    if err:
        log.warning("%s stderr: %s", job.name, err.replace("\n", " ")[:800])
    return proc.returncode, (out or err or "")[:400]


# --------------------------------------------------------------------------
# Loop
# --------------------------------------------------------------------------
class Stop:
    requested = False


def _handle_signal(signum, _frame):  # noqa: ANN001
    log.info("sinal %s recebido: encerrando após a tarefa atual", signum)
    Stop.requested = True


def acquire_lock(state_path: Path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(state_path.name + ".lock")
    # 0600: o lock identifica o PID do agendador e não interessa a ninguém mais.
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    fh = os.fdopen(fd, "w", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise
    fh.truncate(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="scheduler", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--once", action="store_true", help="roda todos os jobs habilitados e sai")
    parser.add_argument("--only-task", action="append", default=None, metavar="JOB",
                        help="com --once: roda apenas os jobs indicados (repetível)")
    parser.add_argument("--only-due", action="store_true",
                        help="com --once: roda só os slots devidos agora (dia/horário da agenda)")
    parser.add_argument("--print-plan", action="store_true", help="mostra horários e sai")
    parser.add_argument("--json", action="store_true", help="com --print-plan: saída JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="registra o que faria, sem executar (não grava estado de execução)")
    parser.add_argument("--no-lock", action="store_true", help="não aplica trava de instância")
    parser.add_argument("--tick", type=int, default=TICK_SECONDS)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, (os.environ.get("AGENDA_LOG_LEVEL") or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        schedule = load_schedule(args.config)
    except ScheduleError as exc:
        log.error("configuração inválida: %s", exc)
        return 2

    state_path = _resolve(args.state)
    config_path = _resolve(args.config)
    dry_run = args.dry_run or (os.environ.get("AGENDA_SCHEDULER_DRY_RUN", "0").strip().lower()
                               in ("1", "true", "yes", "sim"))
    tz = schedule.tz()
    now = datetime.now(tz)
    meta = {
        "schedule_path": str(config_path),
        "schedule_digest": file_digest(config_path),
        "timezone": schedule.timezone,
        "jobs_enabled": [j.name for j in schedule.jobs if j.enabled],
        "jobs_blocked": [j.name for j in schedule.jobs if j.task in DECLARABLE_TASKS],
    }

    if args.print_plan:
        if args.json:
            print(json.dumps({
                "timezone": schedule.timezone,
                "now": now.isoformat(timespec="seconds"),
                "schedule_digest": meta["schedule_digest"],
                "jobs": [{
                    "name": j.name,
                    "task": j.task,
                    "at": j.slot,
                    "days": list(j.days),
                    "days_label": j.days_label,
                    "enabled": j.enabled,
                    "executable": j.task not in DECLARABLE_TASKS,
                    "next_run": next_occurrence(j, now, tz).isoformat(timespec="minutes"),
                } for j in schedule.jobs],
            }, ensure_ascii=False, indent=2))
            return 0
        for job in schedule.jobs:
            if job.task in DECLARABLE_TASKS:
                marker = " (bloqueado: publicação é ato humano)"
            elif not job.enabled:
                marker = " (desabilitado)"
            else:
                marker = ""
            print(f"{job.name:24s} {job.slot} {schedule.timezone} "
                  f"dias={job.days_label:12s} task={job.task}{marker} "
                  f"próximo={next_occurrence(job, now, tz):%Y-%m-%d %H:%M}")
        return 0

    lock = None
    if args.no_lock:
        log.warning("--no-lock: sem trava de instância única — dois processos podem "
                    "duplicar jobs")
    if not args.no_lock:
        try:
            lock = acquire_lock(state_path)
        except OSError:
            log.error("outra instância do agendador está ativa (trava em %s.lock)", state_path)
            return 3

    state = load_state(state_path)
    state.setdefault("jobs", {})
    last_status: Optional[int] = None
    worst = 0

    try:
        if args.once:
            selected: list[tuple[Job, datetime]] = []
            if args.only_task:
                known = {j.name for j in schedule.jobs}
                for name in args.only_task:
                    if name not in known:
                        log.warning("--only-task %s: job não existe na agenda", name)
            for job in schedule.jobs:
                if not job.enabled:
                    continue
                if args.only_task and job.name not in args.only_task:
                    continue
                if args.only_due:
                    slot = due_slot(job, datetime.now(tz), state, tz)
                    if slot is None:
                        log.info("%s: fora do slot/dia da agenda — ignorado (--only-due)",
                                 job.name)
                        continue
                    selected.append((job, slot))
                else:
                    # --once sem --only-due é "rodar agora", inclusive fora do
                    # dia configurado; o slot registrado é hoje.
                    local_now = datetime.now(tz)
                    if local_now.isoweekday() not in job.days:
                        log.warning("%s: fora do dia configurado (%s) — executando "
                                    "por --once", job.name, job.days_label)
                    selected.append((job, local_now))
            for job, slot in selected:
                code, detail = run_task(job, dry_run)
                worst = max(worst, code)
                if not dry_run:
                    state["jobs"][job.name] = {
                        "date": slot.date().isoformat(),
                        "at": datetime.now(tz).isoformat(timespec="seconds"),
                        "status": code,
                        "detail": detail,
                    }
                    last_status = code
                    save_state(state_path, state)
                log.info("%s -> %s (%s)", job.name, code, detail or "-")
            if not dry_run:
                # Em dry-run nada é gravado: um ensaio não pode mascarar um run
                # real perdido nem mover o heartbeat do container.
                write_heartbeat(state_path, state, last_status, {}, meta)
            return worst

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        log.info("agendador iniciado tz=%s jobs=%s dry_run=%s digest=%s",
                 schedule.timezone, [j.name for j in schedule.jobs], dry_run,
                 (meta["schedule_digest"] or "-")[:12])
        while not Stop.requested:
            current = datetime.now(tz)
            due = [(j, s) for j in schedule.jobs
                   if (s := due_slot(j, current, state, tz)) is not None]
            # Slot de hoje vencido além da janela: registra perda para não
            # rodar mais tarde e para o relatório saber que houve buraco.
            for job in schedule.jobs:
                if any(j is job for j, _ in due):
                    continue
                lost = expired_slot(job, current, state, tz)
                if lost is not None:
                    state["jobs"].setdefault(job.name, {})
                    state["jobs"][job.name].update({
                        "date": lost.date().isoformat(),
                        "status": None,
                        "detail": "slot perdido (catch-up expirado)",
                    })
                    save_state(state_path, state)
                    log.warning("%s: slot %s perdido (catch-up expirado)",
                                job.name, lost.strftime("%Y-%m-%d %H:%M"))

            for job, slot in due:
                if Stop.requested:
                    break
                code, detail = run_task(job, dry_run)
                last_status = code
                worst = max(worst, code)
                if not dry_run:
                    state["jobs"][job.name] = {
                        "date": slot.date().isoformat(),
                        "at": datetime.now(tz).isoformat(timespec="seconds"),
                        "status": code,
                        "detail": detail,
                    }
                    save_state(state_path, state)

            next_runs = {j.name: next_run(j, datetime.now(tz), state, tz).isoformat(timespec="minutes")
                         for j in schedule.jobs if j.enabled}
            try:
                write_heartbeat(state_path, state, last_status, next_runs, meta)
            except OSError as exc:
                log.warning("não foi possível gravar heartbeat: %s", exc)

            deadline = time.monotonic() + max(1, args.tick)
            while time.monotonic() < deadline and not Stop.requested:
                time.sleep(min(1.0, deadline - time.monotonic()))
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
            except OSError:
                pass

    log.info("agendador encerrado")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
