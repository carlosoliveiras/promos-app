"""Fluxos independentes por provedor: busca, geração de links e limpeza."""
import logging
import random
import time

from . import db, whatsapp
from .providers import PROVIDERS

log = logging.getLogger(__name__)

FLOW_LABELS = {"busca": "Busca de produtos", "links": "Gerar links de afiliado", "limpeza": "Limpar produtos",
               "postagem": "Postar produtos nos grupos"}


def config(pkey):
    return db.provider_config(pkey, PROVIDERS[pkey].FIELDS)


def busca(pkey):
    prov, cfg = PROVIDERS[pkey], config(pkey)
    terms = db.rows("SELECT term, grupo FROM terms WHERE provider = ? AND active = 1", pkey)
    if not terms:
        return "nenhum termo ativo"
    total, errors = 0, []
    for i, t in enumerate(terms):
        if i:
            time.sleep(prov.SEARCH_DELAY)
        try:
            items = prov.search(t["term"], cfg)
        except Exception as e:
            log.exception("busca %s falhou em %r", pkey, t["term"])
            errors.append(f"{t['term']}: {e}")
            continue
        db.upsert_products(pkey, t["grupo"], items)
        total += len(items)
    return f"{total} produtos de {len(terms)} termos" + (f"; erros: {' | '.join(errors)}" if errors else "")


def links(pkey):
    prov, cfg = PROVIDERS[pkey], config(pkey)
    pending = [r["url"] for r in db.rows(
        "SELECT url FROM products WHERE provider = ? AND COALESCE(affiliate_url, '') = '' AND url IS NOT NULL", pkey)]
    done = 0
    for i in range(0, len(pending), prov.LINK_BATCH):
        if i:
            time.sleep(prov.LINK_DELAY)
        for original, short in prov.generate_links(pending[i:i + prov.LINK_BATCH], cfg).items():
            done += db.execute("UPDATE products SET affiliate_url = ?, status = 'PRONTO' "
                               "WHERE provider = ? AND url = ?", short, pkey, original)
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
    targets = db.rows("SELECT b.token, g.jid, g.name FROM bots b JOIN bot_groups g ON g.bot_id = b.id WHERE b.posts = 1")
    if not url or not targets:
        return "configure a URL do wuzapi e ao menos um bot com grupos"
    products = db.rows("SELECT * FROM products WHERE status = 'PRONTO' AND COALESCE(affiliate_url, '') != '' "
                       "ORDER BY RANDOM() LIMIT ?", count)
    sent, errors, first = 0, [], True
    for p in products:
        try:
            image = whatsapp.image_data_url(p["image"])
        except Exception as e:
            errors.append(f"imagem de {p['external_id']}: {e}")
            continue
        ok = False
        for t in targets:
            if not first:
                sleep(random.uniform(1, 5))
            first = False
            try:
                whatsapp.send_image(url, t["token"], t["jid"], image, caption(p))
                ok, sent = True, sent + 1
            except Exception as e:
                log.exception("falha ao postar em %s", t["jid"])
                errors.append(f"{t['name']}: {e}")
        if ok:
            db.execute("UPDATE products SET status = 'ENVIADO' WHERE id = ?", p["id"])
    return f"{len(products)} produtos, {sent} postagens" + (f"; erros: {' | '.join(errors)}" if errors else "")


FLOWS = {"busca": busca, "links": links, "limpeza": limpeza}
POST_JOB = "whatsapp.postagem"


def default_jobs():
    jobs = {f"{p}.{f}": cron for p, prov in PROVIDERS.items() for f, cron in prov.DEFAULT_CRONS.items()}
    jobs[POST_JOB] = "*/10 10-22 * * *"
    return jobs


def run(job_id):
    if job_id == POST_JOB:
        return postagem()
    pkey, flow = job_id.split(".")
    return FLOWS[flow](pkey)
