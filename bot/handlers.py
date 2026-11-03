"""Command layer.

``handle_update`` is a pure-ish function: it takes a normalised update plus a
``BotContext`` and returns ``OutgoingMessage`` objects.  It never touches the
network, never trusts client-supplied identity, and never raises to the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from database.models import FUSO_BAHIA
from feiras.catalogo import DIAS as DIAS_SEMANA
from feiras.catalogo import normalizar_dia

from .db import AuditRepository, EventRepository, UserRepository
from .models import (
    DomainError,
    Event,
    EventStatus,
    NotFound,
    PermissionDenied,
    PublishBlocked,
    UserRole,
    ValidationError,
    normalize,
)
from .permissions import PermissionService
from .publisher import CONFIRMATION_PHRASE, Publisher
from .telegram_api import IncomingUpdate, OutgoingMessage

HELP_PUBLIC = """Agenda Cultural Salvador

/hoje — eventos de hoje
/amanha — eventos de amanhã
/semana — próximos 7 dias
/fimdesemana — sábado e domingo
/gratis — eventos gratuitos (30 dias)
/musica — música
/teatro — teatro
/cinema — cinema
/exposicoes — exposições e artes visuais
/noite — a partir das 18h
/ufba — eventos na UFBA
/bairro [nome] — por bairro (sem nome: lista os bairros)
/fontes — fontes do catálogo
/feiras [dia|bairro] — feiras livres e culturais (sem argumento: hoje)
/feira SLUG — ficha da feira com a fonte citada
/evento ID — detalhes de um evento
/buscar termo — procura por título, local ou descrição
/ajuda — esta mensagem"""


HELP_EDITOR = """
Área de colaboração:
/novo Título | Local | AAAA-MM-DDTHH:MM | link | Descrição
/meus — seus envios e situação"""

HELP_ADMIN = """
Administração:
/pendentes — fila de revisão
/autorizar ID referência — confirma autorização do organizador
/aprovar ID — aprova para publicação
/rejeitar ID motivo
/publicar ID — simulação (dry-run) por padrão
/publicar ID confirmar PUBLICAR — envio real, se habilitado
/lote — simula a fila aprovada
/auditoria ID — histórico do evento
/corrigir ID campo=valor — corrige um campo do evento
/duplicados — registros históricos de deduplicação
/atualizar — frescor da coleta por fonte (não acessa a rede)
/status — estado do bot
/usuarios — perfis cadastrados
/papel ID perfil — define perfil (public|editor|admin)"""

COMMANDS = {
    "start", "ajuda", "help", "agenda", "evento", "buscar", "novo", "meus",
    "pendentes", "aprovar", "rejeitar", "autorizar", "publicar", "lote",
    "auditoria", "status", "usuarios", "papel",
    "hoje", "amanha", "semana", "fimdesemana", "gratis", "musica", "teatro",
    "cinema", "exposicoes", "noite", "ufba", "bairro", "fontes", "atualizar",
    "corrigir", "duplicados", "feiras", "feira",
}

MAX_ARGS = 4000

# Controles C0 (menos \n e \t), C1 e formatação invisível. Ver sanitize().
_CONTROLE = re.compile(r"[\x00-\x08\x0b\x0c\x0d\x0e-\x1f\x7f-\x9f]")
_INVISIVEIS = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")

# Heurística declarada de linguagem: categoria canônica quando existir, senão
# palavras do título/descrição. Não é classificação semântica — é um recorte
# editorial explícito e conservador (o que casar aparece com o termo buscado).
PALAVRAS_CHAVE: dict[str, tuple[str, ...]] = {
    "musica": (
        "musica", "show", "concerto", "cantor", "canta", "banda", "dj",
        "orquestra", "samba", "pagode", "forro", "axe", "mpb", "rock", "jazz",
        "blues", "recital", "turne", "coral", "sinfonic", "instrumental",
        "violao", "piano", "percussao", "hip hop", "rap", "eletronica",
        "album", "single",
    ),
    "teatro": (
        "teatro", "espetaculo", "peca", "dramaturgia", "monologo", "musical",
        "encenacao", "comedia", "tragedia", "stand up", "standup", "circo",
    ),
    "cinema": (
        "cinema", "filme", "cine", "documentario", "curta", "longa",
        "exibicao", "mostra de cinema", "sessao",
    ),
    "exposicoes": (
        "exposicao", "instalacao", "galeria", "museu", "acervo", "fotografia",
        "artes visuais", "vernissage", "bienal", "colecao", "artista plastico",
    ),
}

UFBA = (
    "ufba", "universidade federal da bahia", "escola de musica",
    "escola de belas artes", "instituto de letras", "reitoria",
    "campus de ondina", "pavilhao de aulas", "teatro do movel",
)

GRATIS = ("gratuit", "gratis", "franca", "free", "sem custo", "sem ingresso")

CORRIGIR_USO = (
    "Uso: /corrigir ID campo=valor (um campo por vez). Campos: "
    "titulo, local, inicio, fim, descricao, url, bairro, categoria, preco, endereco."
)


def sanitize(text: str, limit: int = 700) -> str:
    """Strip control chars.  We never set parse_mode, so markup stays inert text.

    Além dos controles C0, remove C1 (``\\x7f``-``\\x9f``) e caracteres de
    formatação invisíveis (zero-width, marcas de direção bidi, BOM). Sem isso
    um ``\\u202e`` no texto de um evento inverte a ordem visual de uma linha
    inteira na conversa — spoofing de mensagem, não markup do Telegram.
    """
    cleaned = _CONTROLE.sub("", text or "")
    cleaned = _INVISIVEIS.sub("", cleaned)
    return cleaned[:limit]


def fmt_when(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%d/%m/%Y %H:%M")


def hoje_local() -> date:
    """Data corrente em Salvador — a mesma base de ``eventos.data_ref``."""
    return datetime.now(FUSO_BAHIA).date()


def fmt_dia(d: date) -> str:
    return d.strftime("%d/%m")


def _fim_de_semana(base: date) -> tuple[date, date]:
    """Sábado e domingo do fim de semana corrente ou do próximo."""
    if base.weekday() >= 5:  # já é sábado/domingo
        sabado = base - timedelta(days=base.weekday() - 5)
    else:
        sabado = base + timedelta(days=5 - base.weekday())
    return sabado, sabado + timedelta(days=1)


def eh_gratis(preco: str) -> bool:
    """Preço textual do núcleo canônico → gratuito?

    Vazio significa *desconhecido*, não gratuito: o bot não inventa
    gratuidade. Só marca quando a fonte diz isso ou quando o valor é R$ 0.
    """
    p = normalize(preco)
    if not p:
        return False
    if any(chave in p for chave in GRATIS):
        return True
    return bool(re.search(r"\br\s*\$?\s*0([,.]0+)?\b", p))


def casa_linguagem(event: Event, chave: str) -> bool:
    """Recorte por categoria canônica ou palavra-chave declarada."""
    alvo = normalize(event.categoria)
    if alvo and chave in alvo:
        return True
    blob = normalize(f"{event.title} {event.description} {event.categoria}")
    return any(palavra in blob for palavra in PALAVRAS_CHAVE[chave])


def casa_ufba(event: Event) -> bool:
    blob = normalize(f"{event.title} {event.venue} {event.endereco} "
                     f"{event.description} {event.categoria}")
    return any(palavra in blob for palavra in UFBA)


def _event_line(event: Event, index: Optional[int] = None) -> str:
    prefix = f"{index}. " if index is not None else ""
    extra = ""
    if event.bairro:
        extra += f" · {event.bairro}"
    if event.preco:
        extra += f" · {event.preco[:40]}"
    return f"{prefix}#{event.id} {fmt_when(event.starts_at)} — {event.title} ({event.venue}){extra}"


def _limitar(texto: str, limite: int = 3800) -> str:
    """Corta em linha inteira para não estourar o limite de 4096 do Telegram."""
    if len(texto) <= limite:
        return texto
    corte = texto[:limite].rsplit("\n", 1)[0]
    return corte + "\n… (lista truncada; refine com /feiras bairro NOME)"


def _feira_line(feira, index: Optional[int] = None) -> str:
    prefix = f"{index}. " if index is not None else ""
    bairro = f" — {feira.bairro}" if feira.bairro else ""
    dias = f" · {feira.dias_texto}" if feira.dias else ""
    return f"{prefix}{feira.nome}{bairro}{dias} [/feira {feira.slug}]"


def _event_detail(event: Event, include_internal: bool) -> str:
    lines = [
        f"#{event.id} {event.title}",
        f"Quando: {fmt_when(event.starts_at)}"
        + (f" até {fmt_when(event.ends_at)}" if event.ends_at else ""),
        f"Local: {event.venue}",
    ]
    if event.endereco:
        lines.append(f"Endereço: {event.endereco}")
    if event.bairro:
        lines.append(f"Bairro: {event.bairro}")
    if event.categoria:
        lines.append(f"Categoria: {event.categoria}")
    if event.preco:
        lines.append(f"Preço: {event.preco}")
    lines += ["", event.description or "(sem descrição)"]
    if event.source_url:
        lines += ["", f"Fonte: {event.source_url}"]
    if include_internal:
        lines += [
            "",
            "— interno —",
            f"Situação: {event.status.value}",
            f"Autorização: {'sim' if event.authorization_confirmed else 'não'}"
            + (f" ({event.authorization_reference})" if event.authorization_reference else ""),
            f"Enviado por: {event.submitted_by or 'coletor (automático)'}",
        ]
        gaps = event.authorization_gaps()
        if gaps:
            lines.append("Pendências: " + "; ".join(gaps))
    return "\n".join(lines)


class Throttle:
    """Crude per-user command throttle held in memory (single process)."""

    # Acima disso, janelas vencidas são descartadas. Sem poda o dicionário
    # cresce com cada id que já falou ao bot uma vez e nunca é liberado:
    # memória crescendo por tráfego, não por conteúdo.
    _PODAR_A_PARTIR_DE = 1024

    def __init__(self, max_events: int = 30, window_seconds: int = 60):
        self._max = max_events
        self._window = window_seconds
        self._hits: dict[int, list[float]] = {}

    def _podar(self, now: float) -> None:
        vencidos = [
            uid
            for uid, marcas in self._hits.items()
            if not any(now - t < self._window for t in marcas)
        ]
        for uid in vencidos:
            del self._hits[uid]

    def allow(self, user_id: Optional[int], now: Optional[float] = None) -> bool:
        if not user_id:
            return False
        now = now if now is not None else datetime.now(timezone.utc).timestamp()
        if len(self._hits) >= self._PODAR_A_PARTIR_DE:
            self._podar(now)
        bucket = [t for t in self._hits.get(user_id, []) if now - t < self._window]
        if len(bucket) >= self._max:
            self._hits[user_id] = bucket
            return False
        bucket.append(now)
        self._hits[user_id] = bucket
        return True


@dataclass
class BotContext:
    events: EventRepository
    users: UserRepository
    audit: AuditRepository
    permissions: PermissionService
    publisher: Publisher
    throttle: Throttle = field(default_factory=Throttle)
    # Catálogo de feiras (leitura pura). None quando o runtime não o carregou.
    feiras: Optional[object] = None

    def role(self, user_id: Optional[int]) -> UserRole:
        return self.permissions.role_of(user_id)


def _parse_command(text: str) -> tuple[str, str]:
    text = (text or "").strip()
    if not text.startswith("/"):
        return "", ""
    head, _, rest = text.partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    return name, rest.strip()[:MAX_ARGS]


class BotHandlers:
    def __init__(self, ctx: BotContext):
        self.ctx = ctx

    # -- entrypoint -------------------------------------------------------
    def handle_update(self, update: IncomingUpdate) -> list[OutgoingMessage]:
        chat_id = update.chat_id
        try:
            if not self.ctx.throttle.allow(update.user_id):
                return [OutgoingMessage(chat_id, "Muitas mensagens em pouco tempo. Aguarde um minuto.")]
            if update.callback_data:
                return self._callback(chat_id, update)
            name, args = _parse_command(update.text)
            if not name:
                return [OutgoingMessage(chat_id, "Use /ajuda para ver os comandos disponíveis.")]
            handler = getattr(self, f"cmd_{name}", None)
            if handler is None or name not in COMMANDS:
                return [OutgoingMessage(chat_id, "Comando desconhecido. Use /ajuda.")]
            return self._wrap(chat_id, handler, chat_id, update.user_id, args)
        except DomainError as exc:
            return [OutgoingMessage(chat_id, f"Não foi possível concluir: {exc}")]
        except Exception as exc:  # pragma: no cover - defensive
            self.ctx.audit.log(update.user_id, "handler.error", "update",
                               str(update.update_id), "info", repr(exc)[:300])
            return [OutgoingMessage(chat_id, "Erro interno. A equipe foi notificada pelo registro.")]

    def _wrap(self, chat_id: int, handler, chat_id_arg: int, user_id, args: str):
        try:
            return handler(chat_id_arg, user_id, args)
        except PermissionDenied as exc:
            return [OutgoingMessage(chat_id, str(exc))]
        except PublishBlocked as exc:
            details = "\n".join(f"• {r}" for r in exc.reasons)
            return [OutgoingMessage(chat_id, f"Publicação bloqueada:\n{details}")]
        except NotFound as exc:
            return [OutgoingMessage(chat_id, str(exc))]
        except ValidationError as exc:
            return [OutgoingMessage(chat_id, f"Dados inválidos: {exc}")]

    # -- public commands --------------------------------------------------
    def cmd_start(self, chat_id: int, user_id, args: str):
        role = self.ctx.role(user_id)
        text = HELP_PUBLIC
        if role.rank >= UserRole.EDITOR.rank:
            text += HELP_EDITOR
        if role is UserRole.ADMIN:
            text += HELP_ADMIN
        return [OutgoingMessage(chat_id, text)]

    def cmd_ajuda(self, chat_id: int, user_id, args: str):
        return self.cmd_start(chat_id, user_id, args)

    cmd_help = cmd_ajuda

    def cmd_agenda(self, chat_id: int, user_id, args: str):
        limit = 8
        if args.isdigit():
            limit = max(1, min(int(args), 20))
        events = self.ctx.events.list_upcoming(limit=limit)
        if not events:
            return [OutgoingMessage(chat_id, "Nenhum evento aprovado no momento.")]
        lines = ["Próximos eventos", ""]
        lines += [_event_line(e, i) for i, e in enumerate(events, 1)]
        lines += ["", "Detalhes: /evento ID"]
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def cmd_evento(self, chat_id: int, user_id, args: str):
        event_id = _as_int(args)
        event = self.ctx.events.get(event_id)
        role = self.ctx.role(user_id)
        # Visão interna só para admin, ou para o autor humano do envio. Evento
        # ingerido pelo coletor tem ``submitted_by == 0``: sem a checagem de
        # autoria positiva, um envelope com user_id == 0 (que não vem da Bot
        # API, mas chega a este método por qualquer chamador interno) veria
        # referência de autorização e pendências de um evento de terceiro.
        eh_autor = (
            event.submitted_by > 0
            and event.submitted_by == user_id
            and role.rank >= UserRole.EDITOR.rank
        )
        if role is UserRole.ADMIN or eh_autor:
            return [OutgoingMessage(chat_id, _event_detail(event, include_internal=True))]
        if event.status.is_public:
            return [OutgoingMessage(chat_id, _event_detail(event, include_internal=False))]
        raise NotFound(f"Evento {event_id} não encontrado.")

    def cmd_buscar(self, chat_id: int, user_id, args: str):
        term = sanitize(args, 120).strip()
        if len(term) < 2:
            return [OutgoingMessage(chat_id, "Informe ao menos 2 caracteres para buscar.")]
        events = self.ctx.events.search(term, limit=10)
        if not events:
            return [OutgoingMessage(chat_id, f"Nada encontrado para “{term}”.")]
        lines = [f"Resultados para “{term}”", ""]
        lines += [_event_line(e, i) for i, e in enumerate(events, 1)]
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    # -- recortes públicos ------------------------------------------------
    def _publicos(
        self,
        *,
        de: Optional[date] = None,
        ate: Optional[date] = None,
        depois: bool = True,
        bairro: Optional[str] = None,
        limite: int = 20,
    ) -> list[Event]:
        return self.ctx.events.list_public(
            de=de.isoformat() if de else None,
            ate=ate.isoformat() if ate else None,
            depois_de=datetime.now(timezone.utc) if depois else None,
            bairro=bairro,
            limit=limite,
        )

    def _lista(
        self,
        chat_id: int,
        titulo: str,
        eventos: list[Event],
        vazio: str,
        dica: str = "Detalhes: /evento ID",
    ):
        if not eventos:
            return [OutgoingMessage(chat_id, vazio)]
        lines = [titulo, ""]
        lines += [_event_line(e, i) for i, e in enumerate(eventos, 1)]
        if dica:
            lines += ["", dica]
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def _por_chave(self, chave: str, dias: int = 60) -> list[Event]:
        candidatos = self._publicos(de=hoje_local(),
                                    ate=hoje_local() + timedelta(days=dias),
                                    limite=500)
        return [e for e in candidatos if casa_linguagem(e, chave)]

    def cmd_hoje(self, chat_id: int, user_id, args: str):
        hoje = hoje_local()
        eventos = self._publicos(de=hoje, ate=hoje)
        return self._lista(chat_id, f"Hoje · {fmt_dia(hoje)}", eventos,
                           "Nenhum evento aprovado para hoje. Veja /amanha ou /semana.")

    def cmd_amanha(self, chat_id: int, user_id, args: str):
        amanha = hoje_local() + timedelta(days=1)
        eventos = self._publicos(de=amanha, ate=amanha, depois=False)
        return self._lista(chat_id, f"Amanhã · {fmt_dia(amanha)}", eventos,
                           "Nenhum evento aprovado para amanhã. Veja /semana.")

    def cmd_semana(self, chat_id: int, user_id, args: str):
        hoje = hoje_local()
        eventos = self._publicos(de=hoje, ate=hoje + timedelta(days=6))
        return self._lista(chat_id, f"Próximos 7 dias · {fmt_dia(hoje)} a "
                                    f"{fmt_dia(hoje + timedelta(days=6))}",
                           eventos, "Nenhum evento aprovado nos próximos 7 dias.")

    def cmd_fimdesemana(self, chat_id: int, user_id, args: str):
        sabado, domingo = _fim_de_semana(hoje_local())
        eventos = self._publicos(de=sabado, ate=domingo)
        return self._lista(chat_id, f"Fim de semana · {fmt_dia(sabado)} e {fmt_dia(domingo)}",
                           eventos, "Nenhum evento aprovado para o fim de semana.")

    def cmd_gratis(self, chat_id: int, user_id, args: str):
        hoje = hoje_local()
        candidatos = self._publicos(de=hoje, ate=hoje + timedelta(days=30), limite=300)
        eventos = [e for e in candidatos if eh_gratis(e.preco)][:20]
        return self._lista(
            chat_id, "Eventos gratuitos — próximos 30 dias", eventos,
            "Nenhum evento marcado como gratuito nos próximos 30 dias.\n"
            "Só entram eventos cujo campo de preço na fonte diz gratuito/entrada franca.",
            dica="Gratuidade conforme a fonte; confirme no link do evento. Detalhes: /evento ID",
        )

    def cmd_musica(self, chat_id: int, user_id, args: str):
        return self._lista(
            chat_id, "Música — próximos 60 dias", self._por_chave("musica")[:20],
            "Nenhum evento de música aprovado nos próximos 60 dias.")

    def cmd_teatro(self, chat_id: int, user_id, args: str):
        return self._lista(
            chat_id, "Teatro — próximos 60 dias", self._por_chave("teatro")[:20],
            "Nenhum evento de teatro aprovado nos próximos 60 dias.")

    def cmd_cinema(self, chat_id: int, user_id, args: str):
        return self._lista(
            chat_id, "Cinema — próximos 60 dias", self._por_chave("cinema")[:20],
            "Nenhuma sessão de cinema aprovada nos próximos 60 dias.")

    def cmd_exposicoes(self, chat_id: int, user_id, args: str):
        return self._lista(
            chat_id, "Exposições — próximos 60 dias", self._por_chave("exposicoes")[:20],
            "Nenhuma exposição aprovada nos próximos 60 dias.")

    def cmd_noite(self, chat_id: int, user_id, args: str):
        eventos = []
        for evento in self._publicos(de=hoje_local(),
                                     ate=hoje_local() + timedelta(days=7), limite=300):
            try:
                hora = datetime.fromisoformat(evento.starts_at).hour
            except ValueError:
                continue
            if hora >= 18 or hora < 5:
                eventos.append(evento)
        return self._lista(chat_id, "Noite — a partir das 18h (próximos 7 dias)",
                           eventos[:20],
                           "Nenhum evento noturno aprovado nos próximos 7 dias.")

    def cmd_ufba(self, chat_id: int, user_id, args: str):
        candidatos = self._publicos(de=hoje_local(),
                                    ate=hoje_local() + timedelta(days=90), limite=500)
        eventos = [e for e in candidatos if casa_ufba(e)][:20]
        return self._lista(chat_id, "UFBA — próximos 90 dias", eventos,
                           "Nenhum evento da UFBA aprovado nos próximos 90 dias.")

    def cmd_bairro(self, chat_id: int, user_id, args: str):
        termo = sanitize(args, 80).strip()
        if not termo:
            bairros = self.ctx.events.bairros(limit=25)
            if not bairros:
                return [OutgoingMessage(
                    chat_id,
                    "Nenhum bairro registrado: os eventos aprovados não têm bairro "
                    "preenchido. Consulte /hoje, /semana ou /buscar.",
                )]
            lines = ["Bairros com eventos aprovados", ""]
            lines += [f"• {nome} — {n} evento(s)" for nome, n in bairros]
            lines += ["", "Filtre com: /bairro nome"]
            return [OutgoingMessage(chat_id, "\n".join(lines))]
        eventos = self._publicos(de=hoje_local(),
                                 ate=hoje_local() + timedelta(days=90),
                                 bairro=termo, limite=20)
        return self._lista(chat_id, f"Bairro: {termo} — próximos 90 dias", eventos,
                           f"Nenhum evento aprovado em “{termo}” nos próximos 90 dias.\n"
                           "Veja os bairros disponíveis com /bairro.")

    def cmd_fontes(self, chat_id: int, user_id, args: str):
        fontes = self.ctx.events.fontes()
        if not fontes:
            return [OutgoingMessage(
                chat_id,
                "Nenhuma fonte cadastrada neste banco canônico. "
                "O catálogo é preenchido pela coleta (CLI/cron), não por este comando.",
            )]
        lines = [f"Fontes cadastradas ({len(fontes)})", ""]
        for f in fontes:
            quando = fmt_when(f["ultima_coleta"]) if f["ultima_coleta"] else "sem coleta registrada"
            lines.append(
                f"• {f['nome']} — {f['confiabilidade']} · {f['aprovados']} aprovado(s)"
                f" · última coleta: {quando}"
            )
            lines.append(f"  {f['url_base']}")
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    # -- editor -----------------------------------------------------------
    def cmd_novo(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.EDITOR, "event.create")
        parts = [p.strip() for p in args.split("|")]
        if len(parts) < 5:
            raise ValidationError(
                "Formato: /novo Título | Local | AAAA-MM-DDTHH:MM | link | Descrição"
            )
        title, venue, starts_at, url, description = parts[0], parts[1], parts[2], parts[3], " | ".join(parts[4:])
        event = Event(
            title=sanitize(title, 160),
            venue=sanitize(venue, 160),
            starts_at=starts_at,
            source_url=url,
            description=sanitize(description, 1500),
            submitted_by=int(user_id),
            status=EventStatus.PENDING,
        )
        saved = self.ctx.events.add(event)
        self.ctx.audit.log(user_id, "event.create", "event", str(saved.id), "allowed",
                           f"autorização pendente; autor={user_id}")
        return [OutgoingMessage(
            chat_id,
            f"Recebido como #{saved.id}.\n"
            "Situação: aguardando revisão e confirmação de autorização do organizador.\n"
            "Nada será publicado até um administrador aprovar.",
        )]

    def cmd_meus(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.EDITOR, "event.list_own")
        events = self.ctx.events.all_for_author(int(user_id), limit=15)
        if not events:
            return [OutgoingMessage(chat_id, "Você ainda não enviou eventos.")]
        lines = ["Seus envios", ""]
        for e in events:
            lines.append(f"{_event_line(e)} — {e.status.value}")
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    # -- admin ------------------------------------------------------------
    def cmd_pendentes(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "event.review_queue")
        events = self.ctx.events.list_by_status([EventStatus.PENDING], limit=20)
        if not events:
            return [OutgoingMessage(chat_id, "Fila de revisão vazia.")]
        lines = ["Fila de revisão", ""]
        for i, e in enumerate(events, 1):
            auth = "autorizado" if e.authorization_confirmed else "SEM autorização"
            lines.append(f"{_event_line(e, i)} — {auth}")
        lines += ["", "Detalhes: /evento ID · Aprovar: /aprovar ID"]
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def cmd_autorizar(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "event.authorize")
        event_id, _, reference = args.partition(" ")
        if not reference.strip():
            raise ValidationError("Informe a referência da autorização (ex.: e-mail de 01/11).")
        event = self.ctx.events.get(_as_int(event_id))
        updated = Event(**{**event.__dict__,
                           "authorization_confirmed": True,
                           "authorization_reference": sanitize(reference, 300)})
        self.ctx.events.update(updated)
        self.ctx.audit.log(user_id, "event.authorize", "event", str(event.id), "allowed",
                           f"ref={sanitize(reference, 300)}")
        return [OutgoingMessage(chat_id, f"#{event.id} marcado como autorizado pelo organizador.")]

    def cmd_aprovar(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "event.approve")
        event = self.ctx.events.get(_as_int(args))
        if event.status is EventStatus.PUBLISHED:
            return [OutgoingMessage(chat_id, f"#{event.id} já está publicado.")]
        if not event.authorization_confirmed:
            raise ValidationError(
                "Confirme a autorização do organizador antes de aprovar (/autorizar ID referência)."
            )
        updated = Event(**{**event.__dict__, "status": EventStatus.APPROVED})
        self.ctx.events.update(updated)
        self.ctx.audit.log(user_id, "event.approve", "event", str(event.id), "allowed", "")
        return [OutgoingMessage(chat_id, f"#{event.id} aprovado. Publicação continua manual e em dry-run.")]

    def cmd_rejeitar(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "event.reject")
        event_id, _, reason = args.partition(" ")
        if not reason.strip():
            raise ValidationError("Informe o motivo da rejeição.")
        event = self.ctx.events.get(_as_int(event_id))
        # Rejeição é fato auditado: status + motivo + trilha gravados juntos.
        # A proveniência não é apagada e o motivo da ingestão fica na auditoria.
        self.ctx.events.reject(event.id, user_id, sanitize(reason, 300))
        return [OutgoingMessage(chat_id, f"#{event.id} rejeitado.")]

    def cmd_publicar(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "publish.request")
        raw = args.strip()
        match = re.match(r"^(\d+)\s*(.*)$", raw, re.S)
        if not match:
            raise ValidationError("Uso: /publicar ID [confirmar PUBLICAR]")
        event_id = int(match.group(1))
        tail = " ".join(match.group(2).split())
        confirm = None
        execute = False
        if tail:
            # Só a frase completa conta. Aceitar "PUBLICAR" sozinho (ou com
            # qualquer sobra após ela) transformava a confirmação deliberada
            # em uma palavra que um /publicar repetido ou um copiar-e-colar
            # acidental disparava.
            if tail.casefold() == f"confirmar {CONFIRMATION_PHRASE}".casefold():
                execute, confirm = True, CONFIRMATION_PHRASE
            else:
                raise ValidationError(
                    "Para envio real digite exatamente: /publicar ID confirmar PUBLICAR"
                )
        result = self.ctx.publisher.publish(event_id, user_id, dry_run=not execute,
                                            confirmation=confirm)
        if result.outcome == "dry_run":
            return [OutgoingMessage(
                chat_id,
                "DRY-RUN — nada foi enviado.\n\n" + result.preview +
                "\n\nEnvio real: /publicar ID confirmar PUBLICAR",
            )]
        if result.outcome == "skipped":
            return [OutgoingMessage(chat_id, f"#{event_id} já publicado (msg {result.message_id}).")]
        return [OutgoingMessage(chat_id, f"#{event_id} publicado (msg {result.message_id}).")]

    def cmd_lote(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "publish.batch")
        sobra = " ".join(args.split())
        execute = sobra.casefold() == f"confirmar {CONFIRMATION_PHRASE}".casefold()
        if sobra and not execute:
            raise ValidationError(
                "Para envio real digite exatamente: /lote confirmar PUBLICAR"
            )
        results = self.ctx.publisher.publish_pending(
            user_id, dry_run=not execute,
            confirmation=CONFIRMATION_PHRASE if execute else None,
        )
        if not results:
            return [OutgoingMessage(chat_id, "Nenhum evento aprovado na fila.")]
        header = "DRY-RUN — simulação da fila. Nada foi enviado." if not execute else "Fila processada:"
        lines = [header, ""]
        for r in results:
            lines.append(f"#{r.event_id}: {r.outcome} — {r.detail}")
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def cmd_auditoria(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "audit.read")
        event_id = _as_int(args)
        event = self.ctx.events.get(event_id)
        entries = self.ctx.audit.for_entity("event", str(event.id))
        lines = [f"Auditoria #{event.id}", "", _event_line(event), ""]
        for e in entries:
            lines.append(f"{e.created_at} · {e.action} · {e.outcome} · {e.detail}".strip(" ·"))
        if not entries:
            lines.append("(sem registros)")
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def cmd_duplicados(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "event.review_queue")
        eventos = self.ctx.events.list_by_status([EventStatus.DUPLICATE], limit=20)
        if not eventos:
            return [OutgoingMessage(chat_id, "Nenhum registro de duplicata.")]
        lines = ["Duplicatas (registro histórico, não mudam de situação)", ""]
        for i, e in enumerate(eventos, 1):
            lines.append(f"{_event_line(e, i)} — {e.motivo or 'sem motivo'}")
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def cmd_atualizar(self, chat_id: int, user_id, args: str):
        """Frescor da coleta — somente leitura, nunca aciona rede."""
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "status.read")
        fontes = self.ctx.events.fontes()
        counts = self.ctx.events.count_by_status()
        pendentes = counts.get(EventStatus.PENDING.value, 0)
        lines = [
            "Atualização da base (leitura)",
            "",
            f"Eventos pendentes de revisão: {pendentes}",
            "",
        ]
        if not fontes:
            lines.append("Nenhuma fonte cadastrada neste banco.")
        else:
            for f in fontes:
                quando = fmt_when(f["ultima_coleta"]) if f["ultima_coleta"] else "nunca"
                lines.append(
                    f"• {f['nome']} — última coleta: {quando}"
                    f" · sessão: {f['ultimo_status'] or '—'}"
                )
        lines += [
            "",
            "Este comando não coleta nem publica: a coleta roda pela CLI/cron "
            "(collectors.cli) e a publicação continua manual e em dry-run.",
        ]
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def cmd_corrigir(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "event.correct")
        match = re.match(r"^(\d+)\s+([a-zA-Z_]+)\s*=\s*(.*)$", args.strip(), re.S)
        if not match:
            raise ValidationError(CORRIGIR_USO)
        event_id = int(match.group(1))
        campo = match.group(2).lower()
        valor = sanitize(match.group(3), 1500).strip()
        atualizado = self.ctx.events.corrigir(event_id, {campo: valor}, user_id)
        aviso = ""
        if atualizado.status is EventStatus.PUBLISHED:
            aviso = ("\nEvento já publicado: a correção vale para as consultas e o "
                     "registro interno, mas não reenvia nada ao canal.")
        return [OutgoingMessage(
            chat_id,
            f"#{atualizado.id} corrigido: {campo} = “{valor[:120]}”.{aviso}",
        )]

    def cmd_status(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "status.read")
        counts = self.ctx.events.count_by_status()
        st = self.ctx.publisher.status()
        lines = ["Estado do bot", ""]
        for status in EventStatus:
            lines.append(f"{status.value}: {counts.get(status.value, 0)}")
        lines += [
            "",
            f"Piloto automático: desligado",
            f"Execução real de envio: {'HABILITADA' if st['allow_execute'] else 'desativada'}",
            f"Canal alvo: {st['target_chat_id'] or 'não configurado'}",
            f"Limite por lote: {st['max_per_run']}",
        ]
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def cmd_usuarios(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "user.list")
        rows = self.ctx.users.list_all(limit=50)
        if not rows:
            return [OutgoingMessage(chat_id, "Nenhum perfil cadastrado.")]
        lines = ["Perfis", ""]
        for tid, username, role in rows:
            lines.append(f"{tid} @{username or '—'} — {role.value}")
        return [OutgoingMessage(chat_id, "\n".join(lines))]

    def cmd_papel(self, chat_id: int, user_id, args: str):
        self.ctx.permissions.require(user_id, UserRole.ADMIN, "user.set_role")
        event_id, _, role_raw = args.partition(" ")
        try:
            role = UserRole(role_raw.strip().lower())
        except ValueError as exc:
            raise ValidationError("Perfis válidos: public, editor, admin.") from exc
        target = _as_int(event_id)
        if target == user_id and role is not UserRole.ADMIN:
            raise PermissionDenied("Você não pode remover seu próprio acesso administrativo.")
        self.ctx.users.upsert(target, "", role)
        self.ctx.audit.log(user_id, "user.set_role", "user", str(target), "allowed", role.value)
        return [OutgoingMessage(chat_id, f"Perfil de {target} definido como {role.value}.")]

    # -- feiras (consulta pública, leitura pura) --------------------------
    def cmd_feiras(self, chat_id: int, user_id, args: str):
        repo = self.ctx.feiras
        if repo is None:
            return [OutgoingMessage(
                chat_id,
                "Catálogo de feiras não carregado neste ambiente.\n"
                "Operação: python -m feiras.cli carregar",
            )]
        termo = sanitize(args, 120).strip()
        dia = bairro = None
        titulo = ""

        if not termo or termo.lower() == "hoje":
            dia = DIAS_SEMANA[hoje_local().weekday()]
            titulo = f"Feiras hoje ({dia})"
        elif normalizar_dia(termo):
            dia = normalizar_dia(termo)
            titulo = f"Feiras em {dia}"
        elif termo.lower().startswith("bairro "):
            bairro = termo.split(" ", 1)[1].strip()
            titulo = f"Feiras no bairro “{bairro}”"
        else:
            achadas = repo.buscar(termo, limite=10)
            if not achadas:
                return [OutgoingMessage(chat_id, f"Nenhuma feira encontrada para “{termo}”.")]
            linhas = [f"Feiras para “{termo}”", ""]
            linhas += [_feira_line(f, i) for i, f in enumerate(achadas, 1)]
            linhas += ["", "Outros recortes: /feiras · /feiras sabado · /feiras bairro Liberdade"]
            return [OutgoingMessage(chat_id, "\n".join(linhas))]

        linhas_feiras = repo.listar(dia=dia, bairro=bairro, limite=25)
        if not linhas_feiras:
            return [OutgoingMessage(
                chat_id,
                f"{titulo}: nenhuma feira com dia de funcionamento publicado.\n"
                "Feiras sem dia publicado não entram nesta lista (não são inferidas).",
            )]
        linhas = [titulo, ""]
        linhas += [_feira_line(f, i) for i, f in enumerate(linhas_feiras, 1)]
        linhas += ["", "Outros recortes: /feiras sabado · /feiras bairro Liberdade"]
        return [OutgoingMessage(chat_id, _limitar("\n".join(linhas)))]

    def cmd_feira(self, chat_id: int, user_id, args: str):
        repo = self.ctx.feiras
        if repo is None:
            return [OutgoingMessage(chat_id, "Catálogo de feiras não carregado neste ambiente.")]
        alvo = sanitize(args, 80).strip()
        if not alvo:
            raise ValidationError("Uso: /feira SLUG (ex.: /feira sao-joaquim)")
        feira = repo.obter(alvo)
        if feira is None:
            raise NotFound(f"Feira “{alvo}” não encontrada. Use /feiras para listar.")

        linhas = [
            f"#{feira.id} {feira.nome}",
            f"Tipo: {feira.tipo}",
            f"Bairro: {feira.bairro or 'não informado pela fonte'}"
            + (f" ({feira.bairro_base})" if feira.bairro else ""),
            f"Endereço: {feira.endereco or 'não informado'}",
            f"Dias: {feira.dias_texto}",
            f"Situação do dado: {feira.status_verificacao}",
        ]
        if feira.observacao:
            linhas += ["", feira.observacao]
        provas = repo.evidencias(feira.id)[:3]
        if provas:
            linhas += ["", "Fonte citada:"]
            for p in provas:
                linhas.append(f"• {p.fonte} ({p.confiabilidade}): “{p.trecho}”")
            linhas.append(provas[0].fonte_url)
        return [OutgoingMessage(chat_id, _limitar("\n".join(linhas)))]

    # -- callbacks --------------------------------------------------------
    def _callback(self, chat_id: int, update: IncomingUpdate):
        data = update.callback_data or ""
        action, _, value = data.partition(":")
        if action == "ver" and value.isdigit():
            return self.cmd_evento(chat_id, update.user_id, value)
        return [OutgoingMessage(chat_id, "Ação não reconhecida.")]


def _as_int(raw: str) -> int:
    raw = (raw or "").strip().split(" ")[0]
    if not raw.isdigit():
        raise ValidationError("Identificador inválido.")
    return int(raw)
