import html
import random
import re
import time
from urllib.parse import quote

import requests

from .. import db
from . import require
from .mercadolivre import HEADERS, cookie_header

NAME = "Amazon"
TERM_LABEL = "URLs de mais vendidos"
TERM_HELP = ("Uma URL de lista de mais vendidos da Amazon por linha "
             "(ex.: https://www.amazon.com.br/gp/bestsellers/computers).")
FIELDS = [
    {"key": "tag", "label": "Tag de afiliado (ex.: minhaloja-20)", "type": "text"},
    {"key": "cookies", "label": "Cookies da conta de associado (opcional: sem eles o link sai longo, com ?tag=)",
     "type": "textarea", "secret": True},
]
DEFAULT_CRONS = {"busca": "0 6 * * *", "links": "0 7 * * *", "limpeza": "0 0 * * *"}
SEARCH_DELAY = 10
LINK_PAUSE = (2, 6)   # segundos entre um link e outro na SiteStripe
MAX_FAILS = 3         # falhas seguidas na SiteStripe: para de tentar e usa o link longo no resto

BASE = "https://www.amazon.com.br"
SHORT_API = BASE + "/associates/sitestripe/getShortUrl"
MARKETPLACE = "526970"  # amazon.com.br
SESSION_MARK = "at-acbbr"  # sem ele o jar não é uma sessão logada: não sobrescreve o salvo


def _price(text):
    """'R$ 1.299,90' -> 1299.9; numa faixa ('R$ 10,00 - R$ 20,00') fica o menor."""
    m = re.search(r"R\$\s*([\d.]+(?:,\d+)?)", text or "")
    return float(m.group(1).replace(".", "").replace(",", ".")) if m else None


def parse(page):
    found = {}
    for card in page.split('id="gridItemRoot"')[1:]:
        asin = re.search(r'data-asin="([A-Z0-9]{10})"', card)
        img = re.search(r"<img\b[^>]*>", card)
        price = re.search(r'p13n-sc-price[^"]*"[^>]*>([^<]+)<', card)
        if not (asin and img and price):
            continue
        alt = re.search(r'\salt="([^"]*)"', img.group(0))
        src = re.search(r'\ssrc="([^"]+)"', img.group(0))
        name = html.unescape(alt.group(1)).strip() if alt else ""
        value = _price(price.group(1))
        if not (name and src and value is not None):
            continue
        found[asin.group(1)] = {
            "external_id": asin.group(1), "name": name, "price": value,
            "url": f"{BASE}/dp/{asin.group(1)}",
            "image": re.sub(r"\._[^/]*_\.", ".", src.group(1)),  # tira o '._AC_UL300_.': imagem no tamanho original
        }
    return list(found.values())


def search(term, cfg):
    for attempt in range(3):
        try:
            r = requests.get(term, headers=HEADERS, timeout=60)
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1.5)
    if "validateCaptcha" in r.text:
        raise RuntimeError("a Amazon pediu captcha (verificação anti-robô) na busca. "
                           "Espere um pouco e tente de novo; se persistir, a Amazon está bloqueando o IP do servidor.")
    return parse(r.text)


def long_link(url, tag):
    return url.split("?")[0] + "?tag=" + quote(tag)


def session(cfg):
    """Cookies vão no jar: assim os Set-Cookie da Amazon renovam a sessão."""
    s = requests.Session()
    s.headers.update(HEADERS)
    for part in cookie_header(cfg.get("cookies")).split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            s.cookies.set(name.strip(), value.strip(), domain=".amazon.com.br", path="/")
    return s


def save_cookies(s):
    current = {c.name: c.value for c in s.cookies}
    if SESSION_MARK in current:
        db.set_setting("amazon.cookies", "; ".join(f"{k}={v}" for k, v in current.items()))


def short_link(s, long):
    r = s.get(SHORT_API, timeout=60, allow_redirects=False,
              params={"longUrl": long, "marketplaceId": MARKETPLACE},
              headers={"accept": "application/json, text/javascript, */*; q=0.01",
                       "referer": BASE + "/", "x-requested-with": "XMLHttpRequest"})
    r.raise_for_status()
    try:
        short = r.json().get("shortUrl")
    except ValueError:  # sessão caída: a Amazon devolve a página de login em vez do JSON
        short = None
    if not short:
        raise RuntimeError(f"SiteStripe respondeu {r.status_code} sem shortUrl: {r.text[:200]}")
    return short


def generate_links(urls, cfg, sleep=time.sleep):
    """Link curto (amzn.to) pela SiteStripe quando há cookies; senão, ou se ela falhar, o link longo com ?tag=."""
    require(cfg, "tag")
    if not cookie_header(cfg.get("cookies")):
        for url in urls:
            yield url, long_link(url, cfg["tag"]), None
        return
    s, fails, fallbacks = session(cfg), 0, []
    try:
        for i, url in enumerate(urls):
            long = long_link(url, cfg["tag"])
            if fails >= MAX_FAILS:
                fallbacks.append(url)
                yield url, long, None
                continue
            if i:
                sleep(random.uniform(*LINK_PAUSE))
            try:
                short, fails = short_link(s, long), 0
            except (requests.RequestException, RuntimeError) as e:
                short, fails = None, fails + 1
                fallbacks.append(f"{url}: {e}")
            yield url, short or long, None
    finally:
        save_cookies(s)
        if fallbacks:
            db.log("amazon.links", "aviso",
                   f"{len(fallbacks)} links saíram longos (com ?tag=) porque a SiteStripe falhou. "
                   "Se forem muitos, os cookies venceram: exporte de novo e salve em Provedores > Amazon.",
                   "\n".join(fallbacks))
