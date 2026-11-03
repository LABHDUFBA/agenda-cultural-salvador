"""Offline demo: walks the whole lifecycle and prints what the user would see.

    python scripts/demo_offline.py

Never touches the network: the runtime is built with NullTransport, and real
sending stays disabled unless AGENDA_ALLOW_EXECUTE=1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import Settings  # noqa: E402
from bot.models import EventStatus, UserRole  # noqa: E402
from bot.runtime import build_runtime  # noqa: E402
from bot.telegram_api import IncomingUpdate, NullTransport  # noqa: E402

ADMIN, EDITOR, VISITOR = 9001, 9002, 9003


def send(rt, text: str, user_id: int) -> None:
    print(f"\n>>> [{user_id}] {text}")
    out = rt.handlers.handle_update(
        IncomingUpdate(update_id=1, chat_id=1, user_id=user_id, username="", text=text)
    )
    for message in out:
        print(message.text)


def main() -> None:
    rt = build_runtime(Settings.from_env(), NullTransport())
    # A demo é autossuficiente: ela não depende de ADMIN_IDS do ambiente para
    # funcionar (antes, sem `ADMIN_IDS=9001`, todo passo administrativo era
    # negado e a demonstração parecia quebrada).
    rt.context.users.upsert(EDITOR, "produtora", UserRole.EDITOR)
    rt.context.users.upsert(ADMIN, "curadoria", UserRole.ADMIN)

    send(rt, "/ajuda", VISITOR)

    # 1. colaborador envia
    send(rt, "/novo Sarau da Lapinha | Largo da Lapinha | 2027-05-02T18:00 | "
             "https://exemplo.org/sarau | Poesia e música no largo.", EDITOR)

    events = rt.context.events.list_by_status([EventStatus.PENDING])
    if not events:
        print("nada pendente; rode novamente após limpar a base")
        return
    event_id = events[-1].id

    # 2. visitante tenta administrar (negado no servidor)
    send(rt, f"/aprovar {event_id}", VISITOR)
    send(rt, f"/publicar {event_id}", VISITOR)

    # 3. admin confirma autorização e aprova
    send(rt, f"/autorizar {event_id} e-mail da curadoria de 01/11/2026", ADMIN)
    send(rt, f"/aprovar {event_id}", ADMIN)

    # 4. dry-run padrão
    send(rt, f"/publicar {event_id}", ADMIN)

    # 5. tentativa real sem a flag do ambiente -> bloqueada antes do transporte
    send(rt, f"/publicar {event_id} confirmar PUBLICAR", ADMIN)

    send(rt, f"/auditoria {event_id}", ADMIN)
    send(rt, "/status", ADMIN)


if __name__ == "__main__":
    main()
