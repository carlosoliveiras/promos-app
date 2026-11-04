"""Fluxos independentes por provedor: busca, geração de links e limpeza."""
import logging
import random
import time
import traceback

from . import db, whatsapp
from .providers import PROVIDERS

log = logging.getLogger(__name__)

POST_JOB = "whatsapp.postagem"
FLOW_LABELS = {"busca": "Busca de produtos", "links": "Gerar links de afiliado", "limpeza": "Limpar produtos",
               "postagem": "Postar produtos nos grupos"}


def _error(job, message):
    """Registra o erro com o traceback; chame dentro de um except."""
    log.exception("%s: %s", job, message)
    db.log(job, "erro", message, traceback.format_exc())


def config(pkey):
    return db.provider_config(pkey, PROVIDERS[pkey].FIELDS)


def busca(pkey):
    prov, cfg = PROVIDERS[pkey], config(pkey)
    terms = [t for t in db.rows("SELECT term, grupo FROM terms WHERE provider = ? AND active = 1", pkey)
             if not hasattr(prov, "accepts") or prov.accepts(t["term"], cfg)]
    if not terms:
        return "nenhum termo ativo para a configuração atual"
    job, total, failed = f"{pkey}.busca", 0, 0
    for i, t in enumerate(terms):
        if i:
            time.sleep(prov.SEARCH_DELAY)
        try:
            items = prov.search(t["term"], cfg)
        except Exception as e:
            _error(job, f"Termo {t['term']!r}: {e}")
            failed += 1
            continue
        db.upsert_products(pkey, t["grupo"], items)
        if items:
            db.log(job, "info", f"Termo {t['term']!r}: {len(items)} produtos")
        else:
            db.log(job, "aviso", f"Termo {t['term']!r}: nenhum produto encontrado (confira a URL ou os filtros; "
                                 "o layout da página pode ter mudado)")
        total += len(items)
    return f"{total} produtos de {len(terms)} termos" + (f", {failed} com erro" if failed else "")


def links(pkey):
    prov, cfg = PROVIDERS[pkey], config(pkey)
    pending = [r["url"] for r in db.rows(
        "SELECT url FROM products WHERE provider = ? AND COALESCE(affiliate_url, '') = '' AND url IS NOT NULL", pkey)]
    job, done, failed = f"{pkey}.links", 0, []
    for original, short, error in prov.generate_links(pending, cfg):
        if short:  # grava na hora: se o fluxo cair no meio, o que já saiu fica salvo
            done += db.execute("UPDATE products SET affiliate_url = ?, status = 'PRONTO' "
                               "WHERE provider = ? AND url = ?", short, pkey, original)
        else:
            failed.append(f"{original}: {error}")
    if failed:
        db.log(job, "aviso", f"{len(failed)} de {len(pending)} produtos ficaram sem link", "\n".join(failed))
    return f"{done} de {len(pending)} links gerados"


def limpeza(pkey):
    return f"{db.execute('DELETE FROM products WHERE provider = ?', pkey)} produtos removidos"


def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def caption(p):
    lines = [f"*{p['name']}*", ""]
    if p["price_original"]:
        lines.append(f"~De {brl(p['price_original'])}~")
    lines.append(f"*Por {brl(p['price'])}*" + (f" ({p['discount']})" if p["discount"] else ""))
    lines += ["", f"🛒 {p['affiliate_url']}"]
    return "\n".join(lines)


def postagem(_=None, sleep=time.sleep):
    url = db.get_setting("whatsapp.url")
    count = int(db.get_setting("whatsapp.post_count", "1") or 1)
    targets = db.rows("SELECT b.token, b.name AS bot, g.jid, g.name FROM bots b JOIN bot_groups g ON g.bot_id = b.id WHERE b.posts = 1")
    if not url or not targets:
        return "configure a URL do wuzapi e ao menos um bot com grupos"
    products = db.rows("SELECT * FROM products WHERE status = 'PRONTO' AND COALESCE(affiliate_url, '') != '' "
                       "ORDER BY RANDOM() LIMIT ?", count)
    sent, errors, first = 0, 0, True
    for p in products:
        try:
            image = whatsapp.image_data_url(p["image"])
        except Exception as e:
            _error(POST_JOB, f"Imagem de {p['name']!r}: {e}")
            errors += 1
            continue
        ok = False
        for t in targets:
            if not first:
                sleep(random.uniform(1, 5))
            first = False
            try:
                whatsapp.send_image(url, t["token"], t["jid"], image, caption(p))
                ok, sent = True, sent + 1
                db.log(POST_JOB, "info", f"Postado {p['name']!r} em {t['name']} (bot {t['bot']})")
            except Exception as e:
                _error(POST_JOB, f"Grupo {t['name']} (bot {t['bot']}): {e}")
                errors += 1
        if ok:
            db.execute("UPDATE products SET status = 'ENVIADO' WHERE id = ?", p["id"])
    if not products:
        return "nenhum produto PRONTO para postar"
    return f"{len(products)} produtos, {sent} postagens" + (f", {errors} com erro" if errors else "")


FLOWS = {"busca": busca, "links": links, "limpeza": limpeza}


def default_jobs():
    jobs = {f"{p}.{f}": cron for p, prov in PROVIDERS.items() for f, cron in prov.DEFAULT_CRONS.items()}
    jobs[POST_JOB] = "*/10 10-22 * * *"
    return jobs


def job_label(job_id):
    pkey, _, flow = job_id.partition(".")
    name = PROVIDERS[pkey].NAME if pkey in PROVIDERS else {"whatsapp": "WhatsApp", "sistema": "Sistema"}.get(pkey, pkey)
    return f"{name} · {FLOW_LABELS[flow]}" if flow in FLOW_LABELS else name


def run(job_id):
    if job_id == POST_JOB:
        return postagem()
    pkey, flow = job_id.split(".")
    return FLOWS[flow](pkey)
