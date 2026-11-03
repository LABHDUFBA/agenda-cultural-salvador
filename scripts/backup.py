#!/usr/bin/env python3
"""Backup, verificação, retenção e restauração do SQLite.

Usa a API de backup online do sqlite3 (``Connection.backup``), que é segura
com o banco em uso e em modo WAL — não copiamos arquivos soltos, porque
copiar .db/-wal/-shm no meio de uma escrita produz um snapshot inconsistente.

Garantias:
  * o snapshot é gerado em arquivo temporário e só aparece com nome final
    depois de verificado (``PRAGMA integrity_check``) e comprimido;
  * cada arquivo tem sidecar ``.sha256`` para detectar corrupção posterior;
  * ``restore`` exige ``--confirm RESTAURAR`` e não sobrescreve destino
    existente sem ``--force``;
  * ``--dry-run`` em todos os subcomandos de escrita: nada é criado/apagado;
  * nunca apaga nada fora do diretório de backup nem arquivos que não
    correspondam ao padrão ``agenda-*.db.gz``.

Exemplos:
    python scripts/backup.py create --keep 14
    python scripts/backup.py create --dry-run
    python scripts/backup.py list
    python scripts/backup.py verify backups/agenda-20261103T031500Z.db.gz
    python scripts/backup.py prune --keep 14 --dry-run
    python scripts/backup.py restore --file backups/agenda-...db.gz --to data/agenda.db --confirm RESTAURAR
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PATTERN = "agenda-*.db.gz"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DEFAULT_DB = os.environ.get("AGENDA_DB") or "data/agenda.db"
DEFAULT_DIR = os.environ.get("AGENDA_BACKUP_DIR") or "backups"
DEFAULT_KEEP = int(os.environ.get("AGENDA_BACKUP_KEEP") or 14)
CONFIRM_PHRASE = "RESTAURAR"


class BackupError(Exception):
    """Falha operacional com código de saída associado."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def _stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(STAMP_FORMAT)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _integrity(db_file: Path) -> dict:
    """Abre o arquivo e roda integrity_check; devolve metadados do schema."""
    conn = sqlite3.connect(str(db_file))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        events = None
        # Esquema canônico (eventos). Bancos legados do bot usam "events" e
        # continuam contados para que nenhum backup esconda dados antigos.
        for nome in ("eventos", "events"):
            existe = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)
            ).fetchone()
            if existe:
                events = conn.execute(f"SELECT COUNT(*) FROM {nome}").fetchone()[0]
                break
        return {
            "integrity": result,
            "schema_version": version,
            "tables": tables,
            "events": events,
            "ok": result == "ok",
        }
    finally:
        conn.close()


def snapshot(db: Path, dest: Path) -> Path:
    """Copia `db` para o temporário de `dest` usando a API de backup.

    Devolve o caminho do arquivo temporário não comprimido; o chamador é
    responsável por verificar, comprimir e remover.
    """
    if not db.exists():
        raise BackupError(f"banco não encontrado: {db}", 2)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".tmp-{dest.name}")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)          # consistente mesmo com escritores ativos
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return tmp


def _compress(src: Path, dest: Path) -> None:
    """gzip determinístico (mtime=0) e escrita atômica."""
    raw = src.read_bytes()
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-", suffix=".gz")
    try:
        with os.fdopen(fd, "wb") as fh:
            with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
                gz.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, dest)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def backups(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob(PATTERN))


def prune(directory: Path, keep: int, dry_run: bool = False) -> list[str]:
    keep = max(1, keep)
    files = backups(directory)
    doomed = files[:-keep] if len(files) > keep else []
    removed: list[str] = []
    for path in doomed:
        removed.append(path.name)
        if not dry_run:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".sha256").unlink(missing_ok=True)
    return removed


def cmd_create(args) -> int:
    db = _path(args.db)
    directory = _path(args.dir)
    stamp = args.stamp or _stamp()
    dest = directory / f"agenda-{stamp}.db.gz"

    if args.dry_run:
        print(json.dumps({
            "dry_run": True,
            "would_backup": str(db),
            "would_write": str(dest),
            "keep": args.keep,
            "prune_candidates": [p.name for p in prune(directory, args.keep, dry_run=True)],
        }, ensure_ascii=False))
        return 0

    try:
        tmp_db = snapshot(db, dest)
        info = _integrity(tmp_db) if args.verify else {"ok": True, "verified": False}
        if args.verify and not info["ok"]:
            tmp_db.unlink(missing_ok=True)
            raise BackupError(f"integridade falhou no snapshot: {info['integrity']}", 1)
        _compress(tmp_db, dest)
        tmp_db.unlink(missing_ok=True)
        digest = _sha256(dest)
        _write_atomic(dest.with_name(dest.name + ".sha256"),
                      f"{digest}  {dest.name}\n".encode("ascii"))
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"backup falhou: {exc}", 1) from exc

    removed = prune(directory, args.keep, dry_run=False)
    print(json.dumps({
        "created": str(dest),
        "bytes": dest.stat().st_size,
        "sha256": digest,
        "verified": bool(args.verify and info.get("ok")),
        "schema_version": info.get("schema_version"),
        "events": info.get("events"),
        "pruned": removed,
        "retained": len(backups(directory)),
    }, ensure_ascii=False))
    return 0


def cmd_verify(args) -> int:
    target = _path(args.file)
    if not target.exists():
        raise BackupError(f"arquivo não encontrado: {target}", 2)
    sidecar = target.with_name(target.name + ".sha256")
    checksum_ok = None
    if sidecar.exists():
        expected = sidecar.read_text(encoding="ascii").split()[0]
        checksum_ok = expected == _sha256(target)
        if not checksum_ok:
            print(json.dumps({"file": str(target), "ok": False,
                              "reason": "sha256 divergente"}, ensure_ascii=False))
            return 1
    with tempfile.TemporaryDirectory(dir=str(target.parent)) as tmpdir:
        restored = Path(tmpdir) / "verify.db"
        with gzip.open(target, "rb") as gz, restored.open("wb") as out:
            shutil.copyfileobj(gz, out)
        info = _integrity(restored)
    info.update({"file": str(target), "checksum_ok": checksum_ok})
    print(json.dumps(info, ensure_ascii=False))
    return 0 if info["ok"] else 1


def cmd_list(args) -> int:
    directory = _path(args.dir)
    items = []
    for path in backups(directory):
        sidecar = path.with_name(path.name + ".sha256")
        items.append({
            "file": path.name,
            "bytes": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                                .strftime(STAMP_FORMAT),
            "checksum": sidecar.exists(),
        })
    print(json.dumps({"dir": str(directory), "count": len(items), "items": items},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_prune(args) -> int:
    directory = _path(args.dir)
    removed = prune(directory, args.keep, dry_run=args.dry_run)
    remaining = len(backups(directory))
    print(json.dumps({"dir": str(directory), "dry_run": bool(args.dry_run),
                      "removed": removed,
                      "retained": remaining - len(removed) if args.dry_run else remaining},
                     ensure_ascii=False))
    return 0


def cmd_restore(args) -> int:
    source = _path(args.file)
    target = _path(args.to)
    if not source.exists():
        raise BackupError(f"backup não encontrado: {source}", 2)
    if args.confirm != CONFIRM_PHRASE:
        raise BackupError(
            f"restauração exige --confirm {CONFIRM_PHRASE} (evita acidente)", 2
        )
    if target.exists() and not args.force:
        raise BackupError(
            f"destino já existe: {target} (use --force; pare o bot antes)", 2
        )
    if args.dry_run:
        print(json.dumps({"dry_run": True, "would_restore": str(source),
                          "to": str(target), "overwrite": target.exists()},
                         ensure_ascii=False))
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".tmp-restore-{target.name}")
    try:
        with gzip.open(source, "rb") as gz, tmp.open("wb") as out:
            shutil.copyfileobj(gz, out)
            out.flush()
            os.fsync(out.fileno())
        info = _integrity(tmp)
        if not info["ok"]:
            raise BackupError(f"backup corrompido, nada foi restaurado: {info['integrity']}", 1)
        if target.exists():
            # Arquivos WAL/SHM de uma geração antiga corrompem o banco restaurado.
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    except BackupError:
        tmp.unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.Error) as exc:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"restauração falhou: {exc}", 1) from exc

    print(json.dumps({"restored": str(target), "from": str(source),
                      "schema_version": info["schema_version"],
                      "events": info["events"]}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backup", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--dir", default=DEFAULT_DIR)
        p.add_argument("--dry-run", action="store_true")

    p_create = sub.add_parser("create", help="cria snapshot comprimido e poda os antigos")
    common(p_create)
    p_create.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    p_create.add_argument("--no-verify", dest="verify", action="store_false", default=True)
    p_create.add_argument("--stamp", default=None, help="carimbo UTC (testes)")
    p_create.set_defaults(func=cmd_create)

    p_verify = sub.add_parser("verify", help="verifica checksum e integridade de um backup")
    p_verify.add_argument("file")
    p_verify.set_defaults(func=cmd_verify)

    p_list = sub.add_parser("list", help="lista backups existentes")
    p_list.add_argument("--dir", default=DEFAULT_DIR)
    p_list.set_defaults(func=cmd_list)

    p_prune = sub.add_parser("prune", help="aplica retenção")
    common(p_prune)
    p_prune.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    p_prune.set_defaults(func=cmd_prune)

    p_restore = sub.add_parser("restore", help="restaura backup para um caminho")
    p_restore.add_argument("--file", required=True)
    p_restore.add_argument("--to", required=True)
    p_restore.add_argument("--confirm", default="")
    p_restore.add_argument("--force", action="store_true")
    p_restore.add_argument("--dry-run", action="store_true")
    p_restore.set_defaults(func=cmd_restore)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BackupError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
