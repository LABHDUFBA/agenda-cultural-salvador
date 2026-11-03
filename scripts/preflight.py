#!/usr/bin/env python3
"""Preflight de implantação — valida ambiente antes de subir a pilha.

Não abre conexão de rede, não imprime segredo algum (só presença/formato) e
não escreve no banco.  Serve tanto para `docker compose up` quanto para
systemd/bare-metal.

    python scripts/preflight.py                 # modo dev (tolerante)
    python scripts/preflight.py --mode prod     # exige token, canal e admins
    python scripts/preflight.py --scan-repo     # procura credencial versionada
    python scripts/preflight.py --json

Códigos: 0 ok (pode haver avisos), 1 erro bloqueante, 2 uso incorreto.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
# Padrão de token do Telegram em qualquer arquivo: usado na varredura do repo.
TOKEN_SCAN_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{33,}\b")
PLACEHOLDERS = {"", "changeme", "change-me", "xxx", "***", "token", "seu_token",
                "cole_aqui", "todo", "null", "none", "-"}
SENSITIVE_KEYS = ("BOT_TOKEN",)

# Toda variável AGENDA_*/BOT_TOKEN/TARGET_CHAT_ID/... lida pelo código.
REQUIRED_PROD = {
    "BOT_TOKEN": "token da Bot API",
    "TARGET_CHAT_ID": "chat/canal de destino",
    "ADMIN_IDS": "pelo menos um admin de bootstrap",
}


class Report:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, code: str, message: str) -> None:
        self.items.append({"level": level, "code": code, "message": message})

    def ok(self, code: str, message: str) -> None:
        self.add("ok", code, message)

    def warn(self, code: str, message: str) -> None:
        self.add("warn", code, message)

    def error(self, code: str, message: str) -> None:
        self.add("error", code, message)

    @property
    def errors(self) -> list[dict]:
        return [i for i in self.items if i["level"] == "error"]

    @property
    def warnings(self) -> list[dict]:
        return [i for i in self.items if i["level"] == "warn"]


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _writable(path: Path) -> tuple[bool, str]:
    """Pode-se criar/escrever em `path`?  Não cria nada permanentemente."""
    if path.exists():
        if not path.is_dir():
            return False, "existe e não é diretório"
        return os.access(path, os.W_OK), "sem permissão de escrita"
    parent = path
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.exists():
        return False, "nenhum ancestral existe"
    return os.access(parent, os.W_OK), f"ancestral {parent} sem escrita"


def check_env(report: Report, mode: str) -> None:
    env = os.environ
    for key, label in REQUIRED_PROD.items():
        raw = (env.get(key) or "").strip()
        if mode == "prod":
            if raw.lower() in PLACEHOLDERS:
                report.error("env.missing", f"{key} ausente ({label}) em modo prod")
            else:
                report.ok("env.present", f"{key} definida ({label})")
        elif raw.lower() in PLACEHOLDERS:
            report.warn("env.empty", f"{key} vazia — modo dev: publicação/uso real bloqueado")

    token = (env.get("BOT_TOKEN") or "").strip()
    if token and token.lower() not in PLACEHOLDERS:
        if TOKEN_RE.match(token):
            # Nunca imprimir o token, nem parcialmente.
            report.ok("env.token_format", "BOT_TOKEN com formato plausível")
        else:
            report.error("env.token_format", "BOT_TOKEN presente mas fora do formato <id>:<segredo>")

    chat = (env.get("TARGET_CHAT_ID") or "").strip()
    if chat:
        if re.fullmatch(r"-?\d+", chat):
            if mode == "prod" and not chat.startswith("-"):
                report.warn("env.chat", "TARGET_CHAT_ID positivo: confirme se é chat privado e não canal")
            else:
                report.ok("env.chat", "TARGET_CHAT_ID numérico")
        else:
            report.error("env.chat", "TARGET_CHAT_ID não numérico")

    execute = (env.get("AGENDA_ALLOW_EXECUTE") or "0").strip().lower() in ("1", "true", "yes", "sim")
    if execute:
        if not chat or chat.lower() in PLACEHOLDERS:
            report.error("env.execute", "AGENDA_ALLOW_EXECUTE=1 sem TARGET_CHAT_ID: envio real ficaria bloqueado")
        elif mode == "prod":
            report.warn("env.execute", "AGENDA_ALLOW_EXECUTE=1: publicação real habilitada nesta implantação")
        else:
            report.ok("env.execute", "AGENDA_ALLOW_EXECUTE=1 (dry-run ainda depende de --execute + frase)")

    tz = (env.get("TZ") or "").strip()
    if tz:
        if tz == "America/Bahia":
            report.ok("env.tz", "TZ=America/Bahia")
        else:
            report.warn("env.tz", f"TZ={tz} (esperado America/Bahia para horários de Salvador)")
    else:
        report.warn("env.tz", "TZ não definida: agendamento e logs podem usar UTC")


def check_paths(report: Report) -> None:
    from scripts.scheduler import DECLARABLE_TASKS, ScheduleError, load_schedule  # noqa: PLC0415

    db = _resolve(os.environ.get("AGENDA_DB") or "data/agenda.db")
    ok, why = _writable(db.parent)
    (report.ok if ok else report.error)("path.db", f"diretório do banco {db.parent} ({why if not ok else 'ok'})")

    backup_dir = _resolve(os.environ.get("AGENDA_BACKUP_DIR") or "backups")
    ok, why = _writable(backup_dir)
    (report.ok if ok else report.error)(
        "path.backup", f"diretório de backup {backup_dir} ({why if not ok else 'ok'})"
    )

    state = _resolve(os.environ.get("AGENDA_SCHEDULER_STATE") or "data/scheduler_state.json")
    ok, why = _writable(state.parent)
    (report.ok if ok else report.error)(
        "path.state", f"diretório de estado do agendador {state.parent} ({why if not ok else 'ok'})"
    )

    schedule_path = os.environ.get("AGENDA_SCHEDULE") or "config/schedule.json"
    try:
        schedule = load_schedule(schedule_path)
        enabled = [j.name for j in schedule.jobs if j.enabled]
        report.ok("schedule.valid",
                  f"agenda válida: {len(schedule.jobs)} jobs em {schedule.timezone}; "
                  f"habilitados: {', '.join(enabled) or 'nenhum'}")
    except ScheduleError as exc:
        report.error("schedule.invalid", f"agenda inválida: {exc}")
        return

    bloqueadas = [j for j in schedule.jobs if j.task in DECLARABLE_TASKS]
    if bloqueadas:
        report.ok("schedule.publish_blocked",
                  f"{len(bloqueadas)} janela(s) editorial(is) declarada(s) e bloqueada(s) "
                  f"(publicação é ato humano): "
                  f"{', '.join(j.slot for j in bloqueadas)}")
    if any(j.task == "coleta" and j.enabled for j in schedule.jobs):
        report.warn("schedule.network",
                    "job de coleta habilitado: o agendador fará requisição externa")

    # Backup recente? Diretório vazio ou snapshot velho é risco operacional.
    if backup_dir.is_dir():
        snaps = sorted(backup_dir.glob("agenda-*.db.gz"), key=lambda p: p.stat().st_mtime)
        if not snaps:
            report.warn("backup.empty", f"nenhum snapshot em {backup_dir}")
        else:
            age_h = (time.time() - snaps[-1].stat().st_mtime) / 3600
            level = "ok" if age_h <= 36 else "warn"
            getattr(report, level)(
                "backup.age",
                f"último backup há {age_h:.1f}h ({snaps[-1].name})",
            )

    if db.exists():
        report.ok("db.exists", f"banco existente em {db}")
    else:
        report.warn("db.missing", f"banco ainda não existe em {db} (será criado no init)")


def check_repo(report: Report) -> None:
    """Garante que nenhuma credencial está versionada."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.warn("repo.git", f"git indisponível, varredura ignorada: {exc}")
        return
    if tracked.returncode != 0:
        report.warn("repo.git", "diretório não é repositório git; varredura ignorada")
        return

    files = [line for line in tracked.stdout.splitlines() if line.strip()]
    if ".env" in files:
        report.error("repo.env", ".env está versionado — remova do índice (git rm --cached .env)")
    else:
        report.ok("repo.env", ".env ausente do versionamento")

    leaks: list[str] = []
    for name in files:
        path = ROOT / name
        if not path.is_file() or path.stat().st_size > 512_000:
            continue
        if path.suffix in {".gz", ".db", ".png", ".jpg", ".zip", ".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if TOKEN_SCAN_RE.search(text):
            leaks.append(name)
    if leaks:
        report.error("repo.secret", f"padrão de token Telegram em arquivos versionados: {', '.join(leaks)}")
    else:
        report.ok("repo.secret", "nenhum padrão de token em arquivos versionados")

    if (ROOT / ".gitignore").exists():
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        missing = [p for p in (".env", "*.db") if p not in ignored]
        if missing:
            report.warn("repo.gitignore", f".gitignore sem entradas para: {', '.join(missing)}")
        else:
            report.ok("repo.gitignore", ".gitignore cobre .env e bancos")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="preflight", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["dev", "prod"], default="dev")
    parser.add_argument("--scan-repo", action="store_true", help="varre arquivos versionados")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    report = Report()
    check_env(report, args.mode)
    check_paths(report)
    if args.scan_repo:
        check_repo(report)

    if not args.quiet:
        if args.json:
            print(json.dumps({
                "mode": args.mode,
                "ok": not report.errors,
                "errors": report.errors,
                "warnings": report.warnings,
                "items": report.items,
            }, ensure_ascii=False, indent=2))
        else:
            for item in report.items:
                marker = {"ok": "  ok ", "warn": "warn ", "error": "ERRO "}[item["level"]]
                print(f"{marker} {item['message']}")

    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
