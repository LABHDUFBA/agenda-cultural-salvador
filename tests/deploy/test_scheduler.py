"""Agendamento: parsing da agenda, dias, janela de catch-up e execução única."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import REPO, run

sys.path.insert(0, str(REPO))

from scripts.scheduler import (  # noqa: E402
    ALL_DAYS,
    DECLARABLE_TASKS,
    TASKS,
    Job,
    ScheduleError,
    due_slot,
    expired_slot,
    is_due,
    load_schedule,
    next_occurrence,
    next_run,
    parse_at,
    parse_days,
    run_task,
)

TZ = ZoneInfo("America/Bahia")
QUI = 4  # ISO: quinta-feira
DOM = 7  # ISO: domingo


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


# --------------------------------------------------------------------------
# Agenda do repositório
# --------------------------------------------------------------------------
def test_agenda_do_repositorio_e_valida():
    schedule = load_schedule(str(REPO / "config" / "schedule.json"))
    assert schedule.timezone == "America/Bahia"
    assert len(schedule.jobs) == 8
    for job in schedule.jobs:
        assert job.task in TASKS or job.task in DECLARABLE_TASKS
    enabled = [j for j in schedule.jobs if j.enabled]
    assert {j.name for j in enabled} == {
        "backup-diario", "integridade-banco", "relatorio-pendentes",
        "coleta-06h", "coleta-12h", "coleta-17h", "coleta-22h",
    }
    # Rotina solicitada: coleta às 06, 12, 17 e 22, todos os dias, gravando
    # sempre como pendente/rejeitado.
    coleta = [j for j in schedule.jobs if j.task == "coleta"]
    assert {(j.at[0], j.at[1]) for j in coleta} == {(6, 0), (12, 0), (17, 0), (22, 0)}
    for job in coleta:
        assert job.enabled is True
        assert job.days == ALL_DAYS
        assert job.args["gravar"] is True
        assert job.args["limite"] == 50
        assert job.args["fontes"] == []      # vazio = todas as coletáveis


def test_agenda_declara_janela_de_publicacao_07h_qui_dom():
    """Janela editorial solicitada: 07:00, quinta e domingo — bloqueada."""
    schedule = load_schedule(str(REPO / "config" / "schedule.json"))
    janelas = [j for j in schedule.jobs if j.task == "publish"]
    assert len(janelas) == 1
    janela = janelas[0]
    assert (janela.at[0], janela.at[1]) == (7, 0)
    assert janela.days == (QUI, DOM)
    assert janela.enabled is False, "publicação não pode vir habilitada"
    assert janela.task in DECLARABLE_TASKS and janela.task not in TASKS


def test_publicacao_nao_pode_ser_habilitada_na_configuracao():
    with pytest.raises(ScheduleError, match="publicação"):
        load_schedule(raw={"jobs": [
            {"name": "pub", "task": "publish", "at": "06:00", "days": ["thu"], "enabled": True},
        ]})


def test_publicacao_declarada_nunca_executa():
    """Mesmo construindo o Job na mão, a tarefa é recusada (defesa em camadas)."""
    job = Job(name="pub", task="publish", at=(7, 0), enabled=True)
    code, detail = run_task(job, dry_run=False)
    assert code == 4
    assert "não é executável" in detail


def test_tarefas_registradas_sao_locais():
    # argv fixo e sem "sh -c": nada de string de shell montada em runtime.
    exemplos = {"coleta": {"limite": 5, "gravar": True}, "backup": {"keep": 7}}
    for name, builder in TASKS.items():
        argv = builder(exemplos.get(name, {}))
        assert isinstance(argv, list) and argv
        assert "sh" not in argv and "-c" not in argv, name


def test_coleta_usa_rotina_em_lote():
    """Sem `fontes`, a rotina cobre todas as fontes coletáveis do catálogo."""
    argv = TASKS["coleta"]({})
    assert argv[0] == sys.executable
    assert argv[1].endswith("scripts/coleta.py")
    assert "--json" in argv
    assert "--fontes" not in argv
    argv = TASKS["coleta"]({"fontes": ["tca", "sympla_salvador"], "limite": 5, "gravar": True})
    assert argv[argv.index("--fontes") + 1] == "tca,sympla_salvador"
    assert argv[argv.index("--limite") + 1] == "5"
    assert "--gravar" in argv
    assert "--gravar" not in TASKS["coleta"]({})


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def test_parse_at():
    assert parse_at("03:15") == (3, 15)
    assert parse_at(" 00:00 ") == (0, 0)
    for bad in ("24:00", "12:60", "3h", "", "03-15"):
        with pytest.raises(ScheduleError):
            parse_at(bad)


def test_parse_days():
    assert parse_days(None) == ALL_DAYS
    assert parse_days("*") == ALL_DAYS
    assert parse_days("todos") == ALL_DAYS
    assert parse_days(["thu", "sun"]) == (QUI, DOM)
    assert parse_days(["domingo", "quinta"]) == (QUI, DOM)     # normaliza e ordena
    assert parse_days("qui, dom") == (QUI, DOM)
    assert parse_days([4, 7]) == (QUI, DOM)
    with pytest.raises(ScheduleError):
        parse_days("seg-sex")        # faixa não é aceita, só lista explícita
    for bad in ("feriado", [], ["thu", 0], 3, True):
        with pytest.raises(ScheduleError):
            parse_days(bad)


def test_agenda_rejeita_tarefa_desconhecida():
    with pytest.raises(ScheduleError):
        load_schedule(raw={"jobs": [{"name": "x", "task": "rm_rf", "at": "01:00"}]})


def test_agenda_rejeita_duplicata_e_vazio():
    with pytest.raises(ScheduleError):
        load_schedule(raw={"jobs": [
            {"name": "a", "task": "backup", "at": "01:00"},
            {"name": "a", "task": "integrity", "at": "02:00"},
        ]})
    with pytest.raises(ScheduleError):
        load_schedule(raw={"jobs": []})


def test_agenda_rejeita_slot_duplicado_e_nome_invalido():
    with pytest.raises(ScheduleError, match="duplica slot"):
        load_schedule(raw={"jobs": [
            {"name": "a", "task": "backup", "at": "01:00"},
            {"name": "b", "task": "backup", "at": "01:00"},
        ]})
    with pytest.raises(ScheduleError, match="nome de job"):
        load_schedule(raw={"jobs": [{"name": "a b", "task": "backup", "at": "01:00"}]})


def test_agenda_rejeita_fuso_invalido():
    with pytest.raises(ScheduleError):
        load_schedule(raw={"timezone": "Marte/Olympus",
                           "jobs": [{"name": "a", "task": "backup", "at": "01:00"}]})


def test_agenda_inexistente_falha():
    with pytest.raises(ScheduleError):
        load_schedule(str(REPO / "config" / "nao-existe.json"))


@pytest.mark.parametrize("args", [
    {"keep": 0},
    {"keep": "abc"},
    {"keep": 9999},
    {"verify": "sim"},
    {"keep": 14, "extra": 1},
])
def test_agenda_valida_args_do_backup(args):
    with pytest.raises(ScheduleError):
        load_schedule(raw={"jobs": [
            {"name": "b", "task": "backup", "at": "03:15", "args": args},
        ]})


def test_agenda_valida_fontes_e_gravar_da_coleta():
    # args vazios = todas as coletáveis, sem gravar
    ok = load_schedule(raw={"jobs": [
        {"name": "c", "task": "coleta", "at": "06:00"},
    ]})
    assert ok.jobs[0].args == {"fontes": [], "gravar": False}
    # ids precisam existir no catálogo e respeitar o formato
    for fontes in (["TCA; rm -rf /"], ["nao_existe_no_catalogo"]):
        with pytest.raises(ScheduleError):
            load_schedule(raw={"jobs": [
                {"name": "c", "task": "coleta", "at": "06:00", "args": {"fontes": fontes}},
            ]})
    with pytest.raises(ScheduleError):
        load_schedule(raw={"jobs": [
            {"name": "c", "task": "coleta", "at": "06:00", "args": {"gravar": "sim"}},
        ]})
    with pytest.raises(ScheduleError):
        load_schedule(raw={"jobs": [
            {"name": "c", "task": "coleta", "at": "06:00", "args": {"fonte": "tca"}},
        ]})
    ok = load_schedule(raw={"jobs": [
        {"name": "c", "task": "coleta", "at": "06:00",
         "args": {"fontes": ["tca", "sympla_salvador", "tca"], "limite": 5, "gravar": True}},
    ]})
    assert ok.jobs[0].args == {"fontes": ["sympla_salvador", "tca"], "limite": 5, "gravar": True}


def test_agenda_aceita_enabled_falso_mas_rejeita_nao_booleano():
    with pytest.raises(ScheduleError):
        load_schedule(raw={"jobs": [
            {"name": "b", "task": "backup", "at": "03:15", "enabled": "true"},
        ]})


# --------------------------------------------------------------------------
# Janela de disparo
# --------------------------------------------------------------------------
def test_is_due_janela():
    job = Job(name="b", task="backup", at=(3, 15))
    assert is_due(job, local(2026, 11, 3, 3, 15), {}, TZ) is True
    assert is_due(job, local(2026, 11, 3, 2, 59), {}, TZ) is False
    assert is_due(job, local(2026, 11, 3, 8, 0), {}, TZ) is True       # 4h45 depois: catch-up
    assert is_due(job, local(2026, 11, 3, 10, 0), {}, TZ) is False     # 6h45: fora da janela


def test_is_due_uma_vez_por_dia():
    job = Job(name="b", task="backup", at=(3, 15))
    state = {"jobs": {"b": {"date": "2026-11-03", "status": 0}}}
    assert is_due(job, local(2026, 11, 3, 3, 30), state, TZ) is False
    assert is_due(job, local(2026, 11, 4, 3, 30), state, TZ) is True


def test_is_due_respeita_enabled():
    job = Job(name="b", task="backup", at=(3, 15), enabled=False)
    assert is_due(job, local(2026, 11, 3, 3, 30), {}, TZ) is False


def test_is_due_aceita_utc_como_entrada():
    job = Job(name="b", task="backup", at=(3, 15))
    # 06:15 UTC == 03:15 em America/Bahia
    assert is_due(job, datetime(2026, 11, 3, 6, 15, tzinfo=ZoneInfo("UTC")), {}, TZ) is True


def test_job_so_roda_nos_dias_declarados():
    job = Job(name="p", task="backup", at=(6, 0), days=(QUI, DOM))
    # 2026-11-03 é terça; 2026-11-05 é quinta.
    assert is_due(job, local(2026, 11, 5, 6, 0), {}, TZ) is True
    assert is_due(job, local(2026, 11, 3, 6, 0), {}, TZ) is False
    assert is_due(job, local(2026, 11, 8, 6, 0), {}, TZ) is True       # domingo
    assert is_due(job, local(2026, 11, 6, 6, 0), {}, TZ) is False      # sexta


def test_catch_up_atravessa_meia_noite_sem_desalinhar_o_slot():
    """Slot de quinta 22h recuperado na sexta 02h continua sendo o slot de quinta."""
    job = Job(name="p", task="backup", at=(22, 0), days=(QUI,))
    slot = due_slot(job, local(2026, 11, 6, 2, 0), {}, TZ)
    assert slot is not None
    assert slot.date().isoformat() == "2026-11-05"      # quinta, não sexta
    state = {"jobs": {"p": {"date": "2026-11-05", "status": 0}}}
    assert is_due(job, local(2026, 11, 6, 2, 0), state, TZ) is False


def test_next_occurrence():
    job = Job(name="b", task="backup", at=(3, 15))
    assert next_occurrence(job, local(2026, 11, 3, 1, 0), TZ) == local(2026, 11, 3, 3, 15)
    assert next_occurrence(job, local(2026, 11, 3, 5, 0), TZ) == local(2026, 11, 3, 3, 15)
    assert next_occurrence(job, local(2026, 11, 3, 22, 0), TZ) == local(2026, 11, 4, 3, 15)


def test_next_run_avanca_slot_ja_executado():
    job = Job(name="b", task="backup", at=(3, 15))
    state = {"jobs": {"b": {"date": "2026-11-03", "status": 0}}}
    # já rodou hoje → próximo é amanhã, não "hoje 03:15" (que pareceria atraso)
    assert next_run(job, local(2026, 11, 3, 4, 0), state, TZ) == local(2026, 11, 4, 3, 15)
    # sem registro → continua sendo hoje
    assert next_run(job, local(2026, 11, 3, 4, 0), {}, TZ) == local(2026, 11, 3, 3, 15)


def test_next_occurrence_respeita_dias():
    job = Job(name="p", task="publish", at=(6, 0), enabled=False, days=(QUI, DOM))
    # terça 2026-11-03 → próxima quinta 2026-11-05
    assert next_occurrence(job, local(2026, 11, 3, 9, 0), TZ) == local(2026, 11, 5, 6, 0)
    # domingo depois do slot → quinta seguinte
    assert next_occurrence(job, local(2026, 11, 8, 23, 0), TZ) == local(2026, 11, 12, 6, 0)


def test_expired_slot_so_marca_dia_valido():
    job = Job(name="p", task="backup", at=(22, 0), days=(QUI,))
    # sexta: não é dia do job → não marca nada (o slot de quinta é passado)
    assert expired_slot(job, local(2026, 11, 6, 22, 0), {}, TZ) is None
    # quinta dentro da janela → ainda é due, não é "perdido"
    assert expired_slot(job, local(2026, 11, 5, 22, 0), {}, TZ) is None
    # quinta 23:30 com catch-up de 1h → slot de quinta perdido
    lost = expired_slot(job, local(2026, 11, 5, 23, 30), {}, TZ, catchup_hours=1)
    assert lost is not None and lost.date().isoformat() == "2026-11-05"
    # já registrado como rodado → não marca perda
    state = {"jobs": {"p": {"date": "2026-11-05", "status": 0}}}
    assert expired_slot(job, local(2026, 11, 5, 23, 30), state, TZ, catchup_hours=1) is None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_print_plan():
    proc = run(["scripts/scheduler.py", "--print-plan"])
    assert proc.returncode == 0, proc.stderr
    assert "backup-diario" in proc.stdout
    assert "America/Bahia" in proc.stdout
    assert "coleta-06h" in proc.stdout and "coleta-22h" in proc.stdout
    assert "janela-publicacao-07h" in proc.stdout
    assert "bloqueado" in proc.stdout


def test_print_plan_json():
    proc = run(["scripts/scheduler.py", "--print-plan", "--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["timezone"] == "America/Bahia"
    assert data["schedule_digest"]
    janelas = [j for j in data["jobs"] if j["task"] == "publish"]
    assert len(janelas) == 1
    assert all(j["days"] == [4, 7] for j in janelas)
    assert all(j["enabled"] is False and j["executable"] is False for j in janelas)
    coletas = [j for j in data["jobs"] if j["task"] == "coleta"]
    assert [c["at"] for c in coletas] == ["06:00", "12:00", "17:00", "22:00"]
    assert all(c["enabled"] is True and c["executable"] is True for c in coletas)


def test_once_dry_run_nao_executa_nem_grava_estado(db, workdir, env_for):
    state = workdir / "state.json"
    proc = run(["scripts/scheduler.py", "--once", "--dry-run"],
               env=env_for(db, workdir / "backups", AGENDA_SCHEDULER_STATE=str(state)))
    assert proc.returncode == 0, proc.stderr
    assert "[dry-run]" in proc.stderr + proc.stdout
    assert not state.exists(), "dry-run não deve gravar estado de execução"
    assert not (workdir / "backups").exists()


def test_dry_run_mostra_plano_mesmo_com_log_em_warning(db, workdir, env_for):
    """O plano do ensaio vai para stdout, não depende do nível de log."""
    state = workdir / "state.json"
    proc = run(["scripts/scheduler.py", "--once", "--dry-run"],
               env=env_for(db, workdir / "backups", AGENDA_SCHEDULER_STATE=str(state),
                           AGENDA_LOG_LEVEL="WARNING"))
    assert proc.returncode == 0, proc.stderr
    assert "[dry-run] backup-diario" in proc.stdout, proc.stdout
    assert "scripts/backup.py" in proc.stdout
    assert not state.exists()


def test_once_executa_backup_de_verdade(db, workdir, env_for):
    backups = workdir / "backups"
    state = workdir / "state.json"
    # --only-task evita que a suíte entre na rede pelos jobs de coleta; o teste
    # continua exercitando a agenda real do repositório (digest e bloqueios).
    proc = run(["scripts/scheduler.py", "--once", "--only-task", "backup-diario"],
               env=env_for(db, backups, AGENDA_SCHEDULER_STATE=str(state)))
    assert proc.returncode == 0, proc.stderr
    assert list(backups.glob("agenda-*.db.gz")), "job de backup não gerou arquivo"
    heartbeat = state.read_text(encoding="utf-8")
    assert "heartbeat_at" in heartbeat and "backup-diario" in heartbeat
    data = json.loads(heartbeat)
    assert data["schedule_digest"], "heartbeat deve registrar o digest da agenda"
    assert "janela-publicacao-07h" in data["jobs_blocked"]
    assert "janela-publicacao-07h" not in data["jobs_enabled"]
    assert "coleta-06h" in data["jobs_enabled"]


def test_dry_run_mostra_plano_da_coleta_sem_tocar_rede(db, workdir, env_for):
    """Wiring da rotina: o ensaio imprime o argv de coleta e não executa."""
    state = workdir / "state.json"
    proc = run(["scripts/scheduler.py", "--once", "--dry-run",
                "--only-task", "coleta-06h", "--only-task", "coleta-12h",
                "--only-task", "coleta-17h", "--only-task", "coleta-22h"],
               env=env_for(db, workdir / "backups", AGENDA_SCHEDULER_STATE=str(state)))
    assert proc.returncode == 0, proc.stderr
    for nome in ("coleta-06h", "coleta-12h", "coleta-17h", "coleta-22h"):
        assert f"[dry-run] {nome}" in proc.stdout, proc.stdout
    assert "scripts/coleta.py" in proc.stdout
    assert "--gravar" in proc.stdout
    assert not state.exists(), "dry-run não deve gravar estado de execução"


def test_once_com_config_invalida_sai_com_2(workdir, env_for, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"jobs": [{"name": "x", "task": "inventada", "at": "01:00"}]}',
                   encoding="utf-8")
    proc = run(["scripts/scheduler.py", "--once"], env={"AGENDA_SCHEDULE": str(bad)})
    assert proc.returncode == 2


def test_once_com_publicacao_habilitada_nao_publica(workdir, tmp_path):
    """Config que tenta habilitar publicação é recusada antes de qualquer execução."""
    bad = tmp_path / "publish.json"
    bad.write_text(json.dumps({"jobs": [
        {"name": "pub", "task": "publish", "at": "06:00", "days": ["thu"], "enabled": True},
    ]}), encoding="utf-8")
    proc = run(["scripts/scheduler.py", "--once"], env={"AGENDA_SCHEDULE": str(bad)})
    assert proc.returncode == 2
    assert "publicação" in (proc.stdout + proc.stderr)


def test_once_only_due_respeita_dia(db, workdir, env_for, tmp_path):
    """--only-due não roda job fora do dia declarado."""
    sched = tmp_path / "weekday.json"
    sched.write_text(json.dumps({
        "timezone": "America/Bahia",
        "jobs": [{"name": "semanal", "task": "backup", "at": "00:00",
                  "days": ["sat"], "args": {"keep": 2}}],
    }), encoding="utf-8")
    state = workdir / "state.json"
    proc = run(["scripts/scheduler.py", "--once", "--only-due"],
               env=env_for(db, workdir / "backups", AGENDA_SCHEDULER_STATE=str(state),
                           AGENDA_SCHEDULE=str(sched)))
    assert proc.returncode == 0, proc.stderr
    data = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    assert "semanal" not in data.get("jobs", {}), "job fora do dia não pode ser executado"
    assert not list((workdir / "backups").glob("agenda-*.db.gz"))


def test_estado_do_agendador_nao_e_legivel_por_outros(db, workdir, env_for):
    state = workdir / "state.json"
    proc = run(["scripts/scheduler.py", "--once", "--only-task", "backup-diario"],
               env=env_for(db, workdir / "backups", AGENDA_SCHEDULER_STATE=str(state)))
    assert proc.returncode == 0, proc.stderr
    assert (state.stat().st_mode & 0o077) == 0, "estado do agendador deveria ser 0600"


def test_trava_de_instancia_unica(db, workdir, env_for):
    """Duas instâncias não podem rodar o mesmo job ao mesmo tempo."""
    import fcntl  # noqa: PLC0415

    state = workdir / "state.json"
    lock_path = state.with_name(state.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")  # noqa: SIM115
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        proc = run(["scripts/scheduler.py", "--once"],
                   env=env_for(db, workdir / "backups", AGENDA_SCHEDULER_STATE=str(state)))
        assert proc.returncode == 3
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
