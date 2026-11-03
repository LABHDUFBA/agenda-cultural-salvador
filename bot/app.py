"""Long-running entrypoint.

Refuses to start without a token.  Even with a token, publication stays in
dry-run unless AGENDA_ALLOW_EXECUTE=1 *and* an admin types the confirmation
phrase per command.  ``--self-check`` validates the wiring offline.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import Settings
from .runtime import build_runtime
from .telegram_api import HttpTransport, NullTransport

log = logging.getLogger("agenda.bot")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot.app")
    parser.add_argument("--self-check", action="store_true",
                        help="valida banco e handlers sem rede")
    parser.add_argument("--once", action="store_true", help="processa um lote de updates e sai")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.self_check:
        runtime = build_runtime(settings, NullTransport())
        status = runtime.publisher.status()
        counts = runtime.context.events.count_by_status()
        print({"db": settings.db_path, "events": counts, "publisher": status})
        return 0

    if not settings.bot_token:
        print("BOT_TOKEN ausente: o bot não inicia (nenhuma conexão tentada).", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    transport = HttpTransport(settings.bot_token, api_base=settings.api_base)
    runtime = build_runtime(settings, transport)
    if settings.allow_execute and settings.target_chat_id is None:
        log.error("AGENDA_ALLOW_EXECUTE=1 sem TARGET_CHAT_ID: publicação continuará bloqueada.")

    offset = 0
    log.info("iniciado; dry-run=%s", not settings.allow_execute)
    while True:
        try:
            updates = transport.get_updates(offset, timeout=25)
        except Exception as exc:
            log.warning("getUpdates falhou: %s", exc)
            time.sleep(5)
            continue
        for update in updates:
            offset = max(offset, update.update_id + 1)
            for message in runtime.handlers.handle_update(update):
                try:
                    transport.send_message(message)
                except Exception as exc:
                    log.warning("envio falhou chat=%s: %s", message.chat_id, exc)
            # Botão inline: sem isto o cliente mostra "carregando" até expirar.
            if update.callback_id:
                answer = getattr(transport, "answer_callback_query", None)
                if answer is not None:
                    try:
                        answer(update.callback_id)
                    except Exception as exc:
                        log.warning("answerCallbackQuery falhou: %s", exc)
        if args.once:
            return 0
        if not updates:
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
