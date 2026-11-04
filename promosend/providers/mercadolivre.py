import html
import json
import re
import time

import requests

from . import ml_api, require

NAME = "Mercado Livre"
TERM_LABEL = "URLs de categoria"
TERM_HELP = ("Um por linha. Fonte página: URL de listagem (ofertas, categorias, buscas). "
             "Fonte API: ID da categoria (ex.: MLB1055) ou URL com category=MLB...")
EXTRA_TEMPLATE = "ml_api.html"
FIELDS = [
    {"key": "fonte", "label": "Fonte da busca", "type": "select", "default": "pagina",
     "options": [("pagina", "Página do site (raspagem)"), ("api", "API oficial do Mercado Livre (OAuth)")]},
    {"key": "cookies", "label": "Cookies da conta de afiliado (texto ou JSON exportado da extensão)",
     "type": "textarea", "secret": True},
    {"key": "csrf_token", "label": "x-csrf-token (do linkbuilder)", "type": "password", "secret": True},
    {"key": "tag", "label": "Tag de afiliado", "type": "text"},
    {"key": "client_id", "label": "Client ID do app (API oficial)", "type": "text"},
    {"key": "client_secret", "label": "Client Secret do app (API oficial)", "type": "password", "secret": True},
    {"key": "redirect_uri", "label": "Redirect URI cadastrada no app (API oficial; em branco usa a do PromoSend)",
     "type": "text"},
]
DEFAULT_CRONS = {"busca": "0 7 * * *", "links": "0 8 * * *", "limpeza": "0 0 * * *"}
SEARCH_DELAY = 10
LINK_BATCH = 20
LINK_DELAY = 15

LINK_API = "https://www.mercadolivre.com.br/affiliate-program/api/v2/affiliates/createLink"
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


def cookie_header(raw):
    """Aceita o cabeçalho pronto ('a=1; b=2') ou o JSON exportado por extensões de cookies."""
    raw = (raw or "").strip()
    if not raw.startswith("["):
        return raw
    return "; ".join(f"{c['name']}={c['value']}" for c in json.loads(raw) if c.get("name"))


def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(s or ""))).strip()


def _num(text):
    """'R$ 1.299,90' -> 1299.9"""
    try:
        return float(re.sub(r"[^\d,]", "", text or "").replace(",", ".", 1))
    except ValueError:
        return None


def _money(block):
    if not block:
        return None
    frac = re.search(r'data-andes-money-amount-fraction="true"[^>]*>([^<]+)<', block)
    if not frac:
        return None
    cents = re.search(r'data-andes-money-amount-cents="true"[^>]*>([^<]+)<', block)
    return _num(frac.group(1).replace(".", "") + ("," + cents.group(1) if cents else ""))


CARD_START = re.compile(r'<div\b[^>]*class="[^"]*\bpoly-card\b[^"]*"[^>]*>')


def parse_polycard(page):
    starts = [m.start() for m in CARD_START.finditer(page)] + [len(page)]
    for start, end in zip(starts, starts[1:]):
        card = page[start:end]
        title = re.search(r'<a\b([^>]*class="[^"]*\bpoly-component__title\b[^"]*"[^>]*)>([\s\S]*?)</a>', card)
        if not title:
            continue
        href = re.search(r'href="([^"]+)"', title.group(1))
        image = None
        img = re.search(r'<img\b([^>]*class="[^"]*\bpoly-component__picture\b[^"]*"[^>]*)/?>', card)
        if img:
            # imagens lazy-load trazem um placeholder data: no src e a real em data-src
            srcs = re.findall(r'\s(?:data-)?src="([^"]+)"', img.group(1))
            image = next((s for s in srcs if not s.startswith("data:")), None)
        old = re.search(r'<s\s[^>]*andes-money-amount--previous[\s\S]*?</s>', card)
        cur = re.search(r'<div class="poly-price__current">([\s\S]*?)</div>', card)
        disc = re.search(r'<span[^>]*class="[^"]*poly-price__disc_label[^"]*"[^>]*>([\s\S]*?)</span>', card)
        yield {"name": _clean(title.group(2)), "url": html.unescape(href.group(1)) if href else None,
               "image": image, "price": _money(cur and cur.group(1)),
               "price_original": _money(old and old.group(0)), "discount": _clean(disc.group(1)) if disc else None}


CAROUSEL = re.compile(r'<div class="dynamic-carousel__item-container">[\s\S]*?'
                      r'<h3 class="dynamic-carousel__title">([\s\S]*?)</h3>')


def parse_carousel(page):
    for m in CAROUSEL.finditer(page):
        block = m.group(0)
        link = re.search(r'<a class="splinter-link"\s+href="([^"]+)"', block)
        img = re.search(r'<img[^>]*\ssrc="([^"]+)"', block)
        old = re.search(r'<span\s+class="dynamic-carousel__oldprice">([^<]+)</span>', block)
        price = re.search(r'<span class="dynamic-carousel__price">[\s\S]*?<span>([^<]+)</span>'
                          r'(?:\s*<sup class="dynamic-carousel__price-decimals">([^<]+)</sup>)?', block)
        disc = re.search(r'<sup\s+class="dynamic-carousel__discount">([^<]+)</sup>', block)
        yield {"name": _clean(m.group(1)), "url": html.unescape(link.group(1)) if link else None,
               "image": img.group(1) if img else None,
               "price": _num(_clean(price.group(1)) + "," + (price.group(2) or "")) if price else None,
               "price_original": _num(old.group(1)) if old else None,
               "discount": _clean(disc.group(1)) if disc else None}


def parse(page):
    parser = parse_carousel if "dynamic-carousel__item-container" in page else parse_polycard
    found = {}
    for p in parser(page):
        url = p["url"] or ""
        if not (p["name"] and p["image"] and p["price"] is not None
                and "mercadolivre.com.br" in url and "click1.mercadolivre" not in url):
            continue
        p["url"] = re.split(r"[?#]", url)[0]
        mlb = re.search(r"MLB-?\d+", url, re.I)
        p["external_id"] = mlb.group(0).replace("-", "").upper() if mlb else p["url"]
        found[p["external_id"]] = p
    return list(found.values())


BLOCKED = ("/captcha/", "account-verification")


def search(term, cfg):
    if cfg.get("fonte") == "api":
        return ml_api.search(term, cfg)
    for attempt in range(3):
        try:
            r = requests.get(term, timeout=60, headers={**HEADERS, "cookie": cookie_header(cfg.get("cookies"))})
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1.5)
    if any(b in r.url for b in BLOCKED):
        raise RuntimeError(
            "o Mercado Livre bloqueou a busca com verificação anti-robô (captcha) e redirecionou para "
            f"{r.url.split('?')[0]}. Entre na conta pelo navegador, navegue normalmente, exporte os cookies de novo "
            "e salve em Provedores > Mercado Livre. Se persistir, o ML está bloqueando o IP do servidor.")
    return parse(r.text)


def generate_links(urls, cfg):
    require(cfg, "cookies", "csrf_token", "tag")
    r = requests.post(LINK_API, json={"urls": urls, "tag": cfg["tag"]}, timeout=60, headers={
        **HEADERS,
        "accept": "application/json, text/plain, */*",
        "origin": "https://www.mercadolivre.com.br",
        "referer": "https://www.mercadolivre.com.br/afiliados/linkbuilder",
        "x-csrf-token": cfg["csrf_token"],
        "cookie": cookie_header(cfg["cookies"]),
    })
    r.raise_for_status()
    return {u["origin_url"]: u["short_url"] for u in r.json().get("urls", []) if u.get("short_url")}
