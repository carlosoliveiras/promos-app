"""Fluxos independentes por provedor: busca, geração de links e limpeza."""
import logging
import random
import time
import traceback
from html import escape

from . import db, telegram, whatsapp
from .providers import PROVIDERS

log = logging.getLogger(__name__)

POST_JOB = "whatsapp.postagem"
FLOW_LABELS = {"busca": "Busca de produtos", "links": "Gerar links de afiliado", "limpeza": "Limpar produtos",
               "postagem": "Postar produtos nos grupos e canais"}


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
    terms += prov.fixed_terms(cfg) if hasattr(prov, "fixed_terms") else []
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


def caption(p, html=False):
    """Texto do post: marcação do WhatsApp ou HTML do Telegram."""
    b, s, e = ("<b>{}</b>", "<s>{}</s>", escape) if html else ("*{}*", "~{}~", str)
    lines = [b.format(e(p["name"])), ""]
    if p["price_original"]:
        lines.append(s.format(f"De {brl(p['price_original'])}"))
    lines.append(b.format(f"Por {brl(p['price'])}") + (f" ({e(p['discount'])})" if p["discount"] else ""))
    lines += ["", f"🛒 {e(p['affiliate_url'])}"]
    return "\n".join(lines)


def postagem(_=None, sleep=time.sleep):
    url, tg_token = db.get_setting("whatsapp.url"), db.get_setting("telegram.token")
    count = int(db.get_setting("whatsapp.post_count", "1") or 1)
    targets = [("whatsapp", t["token"], t["jid"], f"{t['name']} (bot {t['bot']})") for t in db.rows(
        "SELECT b.token, b.name AS bot, g.jid, g.name FROM bots b JOIN bot_groups g ON g.bot_id = b.id "
        "WHERE b.posts = 1")] if url else []
    if tg_token:
        targets += [("telegram", tg_token, chat, f"{chat} (Telegram)")
                    for chat in db.get_setting("telegram.chats").split()]
    if not targets:
        return "configure um bot do WhatsApp com grupos ou um chat do Telegram"
    products = db.rows("SELECT * FROM products WHERE status = 'PRONTO' AND COALESCE(affiliate_url, '') != '' "
                       "ORDER BY RANDOM() LIMIT ?", count)
    sent, errors, first = 0, 0, True
    for p in products:
        ok, image = False, None
        for kind, token, chat, label in targets:
            if not first:
                sleep(random.uniform(1, 5))
            first = False
            try:
                if kind == "telegram":
                    telegram.send_photo(token, chat, p["image"], caption(p, html=True))
                else:
                    image = image or whatsapp.image_data_url(p["image"])
                    whatsapp.send_image(url, token, chat, image, caption(p))
                ok, sent = True, sent + 1
                db.log(POST_JOB, "info", f"Postado {p['name']!r} em {label}")
            except Exception as e:
                _error(POST_JOB, f"{p['name']!r} em {label}: {e}")
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
    name = PROVIDERS[pkey].NAME if pkey in PROVIDERS else {"whatsapp": "WhatsApp e Telegram", "sistema": "Sistema"}.get(pkey, pkey)
    return f"{name} · {FLOW_LABELS[flow]}" if flow in FLOW_LABELS else name


def run(job_id):
    if job_id == POST_JOB:
        return postagem()
    pkey, flow = job_id.split(".")
    return FLOWS[flow](pkey)
