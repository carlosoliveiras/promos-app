import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, flows

log = logging.getLogger(__name__)
scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1})


def seed_jobs():
    for job_id, cron in flows.default_jobs().items():
        db.execute("INSERT OR IGNORE INTO jobs (id, cron) VALUES (?, ?)", job_id, cron)


def run_job(job_id):
    try:
        status = "ok: " + flows.run(job_id)
    except Exception as e:
        log.exception("job %s falhou", job_id)
        status = f"erro: {e}"
    db.execute("UPDATE jobs SET last_run = datetime('now', 'localtime'), last_status = ? WHERE id = ?",
               status[:2000], job_id)


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
    scheduler.add_job(run_job, args=[job_id], id=f"{job_id}.manual", replace_existing=True)


def start():
    seed_jobs()
    scheduler.start()
    for job in db.rows("SELECT id FROM jobs"):
        try:
            sync(job["id"])
        except ValueError:
            log.error("cron inválido em %s, job não agendado", job["id"])
