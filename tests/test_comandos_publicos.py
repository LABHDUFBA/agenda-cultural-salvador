"""Comandos públicos e administrativos novos — contra o SQLite canônico.

Todos os testes são offline: nenhum transporte é chamado e nada é publicado.
"""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest

from bot.config import Settings
from bot.handlers import hoje_local
from bot.models import Event, EventStatus, UserRole
from bot.runtime import build_runtime
from bot.telegram_api import IncomingUpdate

ADMIN = 9101
STRANGER = 9102
CHANNEL = -1009876543210


class FakeTransport:
    def __init__(self):
        self.messages = []
        self.events = []

    def send_message(self, message):
        self.messages.append(message)
        return f"msg-{len(self.messages)}"

    def send_event(self, event, chat_id):
        self.events.append((event.id, chat_id))
        return f"pub-{len(self.events)}"

    def get_updates(self, offset, timeout=25):
        return []


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "agenda.db"),
        bot_token=None,
        target_chat_id=CHANNEL,
        allow_execute=False,
        max_per_run=3,
        min_interval_seconds=300,
        admin_ids=(ADMIN,),
        api_base="https://api.telegram.org",
    )
    return build_runtime(settings, FakeTransport())


def say(runtime, text: str, user_id: int = STRANGER) -> str:
    out = runtime.handlers.handle_update(
        IncomingUpdate(update_id=1, chat_id=555, user_id=user_id,
                       username="someone", text=text)
    )
    assert out
    return "\n".join(m.text for m in out)


_seq = itertools.count(1)


def add_event(runtime, *, dias: int, hora: str = "20:00", titulo: str | None = None,
              local: str = "Teatro Castro Alves", bairro: str = "",
              categoria: str = "", preco: str = "", descricao: str = "Programação de teste.",
              status: EventStatus = EventStatus.APPROVED) -> Event:
    dia = hoje_local() + timedelta(days=dias)
    return runtime.context.events.add(Event(
        title=titulo or f"Evento {next(_seq)}",
        venue=local,
        starts_at=f"{dia.isoformat()}T{hora}",
        description=descricao,
        source_url="https://exemplo.org/evento",
        submitted_by=0,
        status=status,
        bairro=bairro,
        categoria=categoria,
        preco=preco,
    ))


# -- janelas de data ------------------------------------------------------
def test_hoje_so_lista_aprovados_de_hoje(runtime):
    hoje = add_event(runtime, dias=0, hora="23:30", titulo="Hoje Aprovado")
    add_event(runtime, dias=0, hora="23:40", titulo="Hoje Pendente", status=EventStatus.PENDING)
    add_event(runtime, dias=1, hora="23:00", titulo="Amanhã")

    texto = say(runtime, "/hoje")
    assert "Hoje Aprovado" in texto
    assert "Hoje Pendente" not in texto
    assert "Amanhã" not in texto
    assert f"#{hoje.id}" in texto


def test_amanha_nao_inclui_hoje_nem_depois_de_amanha(runtime):
    add_event(runtime, dias=0, hora="23:50", titulo="Evento de Hoje")
    amanha = add_event(runtime, dias=1, hora="09:00", titulo="Evento de Amanhã")
    add_event(runtime, dias=2, hora="09:00", titulo="Evento Depois")

    texto = say(runtime, "/amanha")
    assert "Evento de Amanhã" in texto
    assert f"#{amanha.id}" in texto
    assert "Evento de Hoje" not in texto
    assert "Evento Depois" not in texto


def test_semana_cobre_sete_dias_e_exclui_o_oitavo(runtime):
    add_event(runtime, dias=0, hora="23:00", titulo="Dia Zero")
    add_event(runtime, dias=6, hora="10:00", titulo="Sétimo Dia")
    add_event(runtime, dias=7, hora="10:00", titulo="Oitavo Dia")

    texto = say(runtime, "/semana")
    assert "Dia Zero" in texto
    assert "Sétimo Dia" in texto
    assert "Oitavo Dia" not in texto


def test_fimdesemana_pega_sabado_e_domingo(runtime):
    base = hoje_local()
    dias_ate_sabado = (5 - base.weekday()) % 7
    sabado = add_event(runtime, dias=dias_ate_sabado, hora="19:00", titulo="Sábado Show")
    domingo = add_event(runtime, dias=dias_ate_sabado + 1, hora="11:00", titulo="Domingo Show")
    add_event(runtime, dias=dias_ate_sabado + 2, hora="11:00", titulo="Segunda Show")

    texto = say(runtime, "/fimdesemana")
    assert f"#{sabado.id}" in texto and f"#{domingo.id}" in texto
    assert "Segunda Show" not in texto


# -- filtros editoriais ---------------------------------------------------
def test_gratis_usa_apenas_preco_declarado(runtime):
    franca = add_event(runtime, dias=3, titulo="Entrada Franca", preco="Entrada franca")
    zero = add_event(runtime, dias=4, titulo="Zero Reais", preco="R$ 0,00")
    add_event(runtime, dias=5, titulo="Pago", preco="R$ 40")
    add_event(runtime, dias=6, titulo="Preço Desconhecido", preco="")

    texto = say(runtime, "/gratis")
    assert f"#{franca.id}" in texto and f"#{zero.id}" in texto
    assert "Pago" not in texto
    assert "Preço Desconhecido" not in texto


def test_recortes_por_linguagem(runtime):
    musica = add_event(runtime, dias=10, titulo="Concerto da Orquestra")
    teatro = add_event(runtime, dias=11, titulo="Espetáculo de Teatro")
    cinema = add_event(runtime, dias=12, titulo="Sessão de Cinema")
    exposicao = add_event(runtime, dias=13, titulo="Exposição de Fotografia")
    outro = add_event(runtime, dias=14, titulo="Feira de Artesanato")

    for comando, esperado in (
        ("/musica", musica), ("/teatro", teatro),
        ("/cinema", cinema), ("/exposicoes", exposicao),
    ):
        texto = say(runtime, comando)
        assert f"#{esperado.id}" in texto, comando
        assert f"#{outro.id}" not in texto, comando


def test_categoria_canonica_tambem_casa(runtime):
    show = add_event(runtime, dias=15, titulo="Apresentação X", categoria="Música")
    assert f"#{show.id}" in say(runtime, "/musica")


def test_noite_filtra_horario_local(runtime):
    noite = add_event(runtime, dias=2, hora="20:30", titulo="Show da Noite")
    tarde = add_event(runtime, dias=2, hora="15:00", titulo="Tarde Cultural")

    texto = say(runtime, "/noite")
    assert f"#{noite.id}" in texto
    assert f"#{tarde.id}" not in texto


def test_ufba_por_local(runtime):
    ufba = add_event(runtime, dias=5, local="Escola de Música da UFBA",
                     titulo="Recital de Formatura")
    outro = add_event(runtime, dias=5, local="Teatro Castro Alves", titulo="Show Qualquer")
    texto = say(runtime, "/ufba")
    assert f"#{ufba.id}" in texto
    assert f"#{outro.id}" not in texto


def test_bairro_lista_e_filtra_sem_acento(runtime):
    add_event(runtime, dias=7, bairro="Pelourinho", titulo="Evento no Centro Histórico")
    add_event(runtime, dias=8, bairro="Ondina", titulo="Evento na Orla")

    lista = say(runtime, "/bairro")
    assert "Pelourinho" in lista and "Ondina" in lista

    filtrado = say(runtime, "/bairro pelourinho")
    assert "Evento no Centro Histórico" in filtrado
    assert "Evento na Orla" not in filtrado


def test_fontes_le_catalogo_canonico(runtime):
    runtime.conn.execute(
        "INSERT INTO fontes (nome, url_base, tipo, confiabilidade, criado_em) "
        "VALUES ('TCA', 'https://www.tca.ba.gov.br', 'site', 'alta', '2026-11-01T10:00:00+00:00')"
    )
    runtime.conn.commit()
    texto = say(runtime, "/fontes")
    assert "TCA" in texto
    assert "tca.ba.gov.br" in texto
    assert "alta" in texto


def test_fontes_vazio_explica_origem(runtime):
    texto = say(runtime, "/fontes")
    assert "Nenhuma fonte cadastrada" in texto


# -- administração --------------------------------------------------------
def test_atualizar_e_somente_leitura_e_exige_admin(runtime):
    negado = say(runtime, "/atualizar")
    assert "restrita" in negado.lower() or "permissão" in negado.lower()

    texto = say(runtime, "/atualizar", ADMIN)
    assert "não coleta nem publica" in texto
    assert "pendentes" in texto.lower()
    assert runtime.transport.events == []


def test_corrigir_atualiza_campo_e_auditoria(runtime):
    evento = add_event(runtime, dias=20, titulo="Título Errado", bairro="")
    texto = say(runtime, f"/corrigir {evento.id} titulo=Título Correto", ADMIN)
    assert "corrigido" in texto

    atualizado = runtime.context.events.get(evento.id)
    assert atualizado.title == "Título Correto"
    assert atualizado.title == atualizado.validated().title

    detalhe = say(runtime, f"/auditoria {evento.id}", ADMIN)
    assert "event.correct" in detalhe
    assert "Título Errado" in detalhe


def test_corrigir_bairro_aparece_no_filtro_publico(runtime):
    evento = add_event(runtime, dias=21, titulo="Evento Sem Bairro")
    say(runtime, f"/corrigir {evento.id} bairro=Rio Vermelho", ADMIN)
    assert "Evento Sem Bairro" in say(runtime, "/bairro rio vermelho")


def test_corrigir_rejeita_campo_invalido_e_nao_admin(runtime):
    evento = add_event(runtime, dias=22)
    assert "Campo não corrigível" in say(runtime, f"/corrigir {evento.id} status=aprovado", ADMIN)
    assert "permissão" in say(runtime, f"/corrigir {evento.id} titulo=Hack").lower()

    uso = say(runtime, f"/corrigir {evento.id}", ADMIN)
    assert "Uso:" in uso


def test_corrigir_valida_data_e_nao_publica(runtime):
    evento = add_event(runtime, dias=23, titulo="Data Errada")
    ruim = say(runtime, f"/corrigir {evento.id} inicio=31/12/2026", ADMIN)
    assert "data" in ruim.lower()
    assert runtime.transport.events == []


def test_duplicados_exige_admin_e_explica_vazio(runtime):
    assert "permissão" in say(runtime, "/duplicados").lower()
    assert "Nenhum registro de duplicata" in say(runtime, "/duplicados", ADMIN)


def test_ajuda_documenta_os_novos_comandos(runtime):
    texto = say(runtime, "/ajuda")
    for comando in ("/hoje", "/amanha", "/semana", "/fimdesemana", "/gratis",
                    "/musica", "/teatro", "/cinema", "/exposicoes", "/noite",
                    "/ufba", "/bairro", "/fontes"):
        assert comando in texto
