import logging
import time
import traceback

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, flows

log = logging.getLogger(__name__)
scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1})


def seed_jobs():
    for job_id, cron in flows.default_jobs().items():
        db.execute("INSERT OR IGNORE INTO jobs (id, cron) VALUES (?, ?)", job_id, cron)
    old = db.row("SELECT * FROM jobs WHERE id = ?", flows.POPULAR_JOB)
    if old:  # migra o Popular Grupo único para um job por bot, com a mesma configuração
        for bid in flows.popular_bot_ids():
            db.execute("INSERT OR IGNORE INTO jobs (id, cron, enabled) VALUES (?, ?, ?)",
                       flows.popular_job(bid), old["cron"], old["enabled"])
            for k in ("group", "group_name", "count"):
                if db.get_setting(f"popular.{k}") and not db.get_setting(f"popular.{bid}.{k}"):
                    db.set_setting(f"popular.{bid}.{k}", db.get_setting(f"popular.{k}"))
        db.execute("DELETE FROM jobs WHERE id = ?", flows.POPULAR_JOB)


def run_job(job_id, origem="agendado"):
    start_id = db.row("SELECT COALESCE(MAX(id), 0) AS m FROM logs")["m"]
    started = time.monotonic()
    details = None
    try:
        result = flows.run(job_id)
        failures = db.row("SELECT COUNT(*) AS n FROM logs WHERE id > ? AND job = ? AND level = 'erro'",
                          start_id, job_id)["n"]
        level, status = ("aviso" if failures else "ok"), "ok: " + result
    except Exception as e:
        log.exception("job %s falhou", job_id)
        level, status, details = "erro", f"erro: {e}", traceback.format_exc()
    db.execute("UPDATE jobs SET last_run = datetime('now', 'localtime'), last_status = ? WHERE id = ?",
               status[:2000], job_id)
    db.log(job_id, level, f"Execução {origem} em {time.monotonic() - started:.1f}s: {status.split(': ', 1)[1]}", details)
    db.prune_logs()


def trigger(cron):
    return CronTrigger.from_crontab(cron)  # ValueError se a expressão for inválida


def sync(job_id):
    """Aplica no agendador o cron/ativo salvo no banco."""
    job = db.row("SELECT * FROM jobs WHERE id = ?", job_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if job and job["enabled"]:
        scheduler.add_job(run_job, trigger(job["cron"]), args=[job_id], id=job_id)


def run_now(job_id):
    scheduler.add_job(run_job, args=[job_id, "manual"], id=f"{job_id}.manual", replace_existing=True)


def start():
    seed_jobs()
    scheduler.start()
    for job in db.rows("SELECT id FROM jobs"):
        try:
            sync(job["id"])
        except ValueError:
            log.error("cron inválido em %s, job não agendado", job["id"])
            db.log(job["id"], "erro", "Cron inválido: o fluxo não foi agendado")
