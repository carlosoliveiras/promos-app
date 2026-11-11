import html
import json
import random
import re
import time

import requests

from .. import db
from . import ml_api, require

NAME = "Mercado Livre"
TERM_LABEL = "URLs de categoria"
TERM_HELP = ("Uma URL de listagem do Mercado Livre por linha (ofertas, categorias, buscas), lida pela página "
             "com os cookies. As categorias da API (MLB1055) ficam no painel da API, abaixo.")
EXTRA_TEMPLATE = "ml_api.html"
FIELDS = [
    {"key": "hub", "label": "Buscar produtos do hub de afiliados (mercadolivre.com.br/afiliados/hub)",
     "type": "select", "default": "nao", "options": [("nao", "Não"), ("sim", "Sim")]},
    {"key": "hub_qtd", "label": "Quantidade de produtos do hub por busca (máx. 1000)", "type": "number",
     "default": "100"},
    {"key": "cookies", "label": "Cookies da conta de afiliado (texto, JSON da extensão ou \"Copy as cURL\")",
     "type": "textarea", "secret": True},
    {"key": "csrf_token", "label": "x-csrf-token (opcional: o sistema busca sozinho no linkbuilder)",
     "type": "password", "secret": True},
    {"key": "tag", "label": "Tag de afiliado", "type": "text"},
    {"key": "client_id", "label": "Client ID do app (API oficial)", "type": "text"},
    {"key": "client_secret", "label": "Client Secret do app (API oficial)", "type": "password", "secret": True},
    {"key": "redirect_uri", "label": "Redirect URI cadastrada no app (API oficial; em branco usa a do PromoSend)",
     "type": "text"},
]
DEFAULT_CRONS = {"busca": "0 7 * * *", "links": "0 8 * * *", "limpeza": "0 0 * * *"}
SEARCH_DELAY = 10
LINK_PAUSE = (5, 14)  # segundos entre um link e outro, como faz quem gera na mão
MAX_AUTH_ERRORS = 5   # 401/403 seguidos mesmo com csrf novo: a sessão caiu

BASE = "https://www.mercadolivre.com.br"
LINK_API = BASE + "/affiliate-program/api/v2/affiliates/createLink"
LINKBUILDER = BASE + "/afiliados/linkbuilder"
HUB = BASE + "/afiliados/hub"
HUB_API = BASE + "/affiliate-program/api/hub/search"
HUB_GROUP = "Hub de afiliados"
HUB_MAX = 1000        # o próprio hub para de carregar no scroll em 1000
HUB_PAUSE = (2, 5)    # segundos entre uma página do scroll e outra
SESSION_MARKS = ("orguseridp", "ssid")  # sem eles o jar não é uma sessão logada: não sobrescreve o salvo
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


def cookie_header(raw):
    """Aceita o cabeçalho pronto ('a=1; b=2'), o JSON exportado por extensões ou um "Copy as cURL"."""
    raw = (raw or "").strip()
    if raw.startswith("["):
        return "; ".join(f"{c['name']}={c['value']}" for c in json.loads(raw) if c.get("name"))
    if raw.lower().startswith("curl "):
        m = (re.search(r"""(?:-H|--header)\s+(['"])\s*cookie\s*:\s*(.*?)\1""", raw, re.I | re.S)
             or re.search(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", raw, re.S))
        if not m:
            raise RuntimeError("não achei o cookie no cURL colado")
        return m.group(2).strip()
    return raw


def session(cfg):
    """Cookies vão no jar, não num cabeçalho fixo: assim os Set-Cookie do ML renovam a sessão."""
    s = requests.Session()
    s.headers.update(HEADERS)
    for part in cookie_header(cfg.get("cookies")).split(";"):
        name, sep, value = part.strip().partition("=")
        if sep:
            s.cookies.set(name.strip(), value.strip(), domain=".mercadolivre.com.br", path="/")
    return s


def save_cookies(s):
    current = {c.name: c.value for c in s.cookies}
    if all(m in current for m in SESSION_MARKS):
        db.set_setting("mercadolivre.cookies", "; ".join(f"{k}={v}" for k, v in current.items()))


def _check_blocked(r, what):
    if any(b in r.url for b in BLOCKED):
        raise RuntimeError(
            f"o Mercado Livre bloqueou {what} com verificação anti-robô (captcha) e redirecionou para "
            f"{r.url.split('?')[0]}. Entre na conta pelo navegador, navegue normalmente, exporte os cookies de novo "
            "e salve em Provedores > Mercado Livre. Se persistir, o ML está bloqueando o IP do servidor.")


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


NORDIC = re.compile(r'<script[^>]*id="__NORDIC_RENDERING_CTX__"[^>]*>\s*_n\.ctx\.r=([\s\S]*?)</script>')


def _polycards(node):
    if isinstance(node, dict):
        yield from node.get("polycards") or []
        node = list(node.values())
    if isinstance(node, list):
        for v in node:
            yield from _polycards(v)


def from_polycard(card):
    """Card do JSON polycard (hub de afiliados e páginas montadas no navegador)."""
    comp = {c.get("type"): c for c in card.get("components") or []}
    price = (comp.get("price") or {}).get("price") or {}
    pics = (card.get("pictures") or {}).get("pictures") or []
    chip = (comp.get("chip") or {}).get("chip") or {}
    rate = re.search(r"(\d+(?:[.,]\d+)?)\s*%", " ".join((chip.get(k) or {}).get("text", "") for k in ("pill", "label")))
    url = (card.get("metadata") or {}).get("url")
    return {"name": ((comp.get("title") or {}).get("title") or {}).get("text"),
            "url": "https://" + url.removeprefix("https://") if url else None,
            "image": f"https://http2.mlstatic.com/D_NQ_NP_{pics[0]['id']}-O.jpg" if pics else None,
            "price": (price.get("current_price") or {}).get("value"),
            "price_original": (price.get("previous_price") or {}).get("value"),
            "discount": (price.get("discount_label") or {}).get("text"),
            "commission": float(rate.group(1).replace(",", ".")) / 100 if rate else None}


def parse_nordic(page):
    """Páginas que montam os cards no navegador trazem os produtos num JSON embutido."""
    m = NORDIC.search(page)
    if not m:
        return []
    try:
        data = json.JSONDecoder().raw_decode(m.group(1))[0]
    except ValueError:
        return []
    return map(from_polycard, _polycards(data))


def keep(items):
    """Descarta cards incompletos ou de anúncio e deduplica pelo MLB."""
    found = {}
    for p in items:
        url = p["url"] or ""
        if not (p["name"] and p["image"] and p["price"] is not None
                and "mercadolivre.com.br" in url and "click1.mercadolivre" not in url):
            continue
        p["url"] = re.split(r"[?#]", url)[0]
        mlb = re.search(r"MLBU?-?\d+", url, re.I)
        p["external_id"] = mlb.group(0).replace("-", "").upper() if mlb else p["url"]
        found[p["external_id"]] = p
    return list(found.values())


def parse(page):
    if "dynamic-carousel__item-container" in page:
        return keep(parse_carousel(page))
    if CARD_START.search(page):
        return keep(parse_polycard(page))
    return keep(parse_nordic(page))


BLOCKED = ("/captcha/", "account-verification")


def fixed_terms(cfg):
    """O hub de afiliados não é cadastrado como termo: liga e desliga na configuração."""
    return [{"term": HUB, "grupo": HUB_GROUP}] if cfg.get("hub") == "sim" else []


def hub_search(cfg, sleep=time.sleep):
    """Recomendações do hub, paginadas como no scroll da página, até a quantidade configurada."""
    try:
        qtd = min(int(float(cfg.get("hub_qtd") or 0)), HUB_MAX)
    except ValueError:
        raise RuntimeError("quantidade do hub inválida: use um número de 1 a 1000")
    s, found, offset = session(cfg), {}, 0
    headers = {"x-custom-origin": BASE, "origin": BASE, "referer": HUB}
    while len(found) < qtd:
        if offset:
            sleep(random.uniform(*HUB_PAUSE))
        r = s.post(HUB_API, params={"is_affiliate": "true", "device": "desktop"},
                   json={"offset": offset, "filters": []}, headers=headers, timeout=60)
        _check_blocked(r, "o hub de afiliados")
        if "/login" in r.url or "json" not in r.headers.get("content-type", ""):
            raise RuntimeError("os cookies do Mercado Livre expiraram: o hub de afiliados caiu no login. "
                               "Exporte os cookies de novo e salve em Provedores > Mercado Livre.")
        r.raise_for_status()
        cards = (r.json().get("polycard_client_model") or {}).get("polycards") or []
        if not cards:
            break
        before = len(found)
        found.update({p["external_id"]: p for p in keep(map(from_polycard, cards))})
        if len(found) == before:  # página só com repetidos: o hub acabou
            break
        offset += len(cards)
    save_cookies(s)
    return list(found.values())[:qtd]


def accepts(term, cfg):
    """Categorias (MLB1055) vão pela API, que só roda se estiver autorizada; URLs vão pela página."""
    return not ml_api.is_category(term) or ml_api.status()["authorized"]


def search(term, cfg):
    if ml_api.is_category(term):
        return ml_api.search(term, cfg)
    if term == HUB:
        return hub_search(cfg)
    s = session(cfg)
    for attempt in range(3):
        try:
            r = s.get(term, timeout=60)
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(1.5)
    _check_blocked(r, "a busca")
    save_cookies(s)
    return parse(r.text)


def get_csrf(s):
    """O ML embute o token em qualquer página logada; o linkbuilder sempre tem."""
    r = s.get(LINKBUILDER, timeout=60)
    r.raise_for_status()
    _check_blocked(r, "a página de afiliados")
    if "/login" in r.url or "/jms/" in r.url:
        raise RuntimeError("os cookies do Mercado Livre expiraram: a página de afiliados caiu no login. "
                           "Exporte os cookies de novo e salve em Provedores > Mercado Livre.")
    for pattern in (r'<meta\s+name="csrf-token"\s+content="([^"]+)"', r'"csrfToken"\s*:\s*"([^"]+)"',
                    r'name="_csrf"\s+value="([^"]+)"'):
        m = re.search(pattern, r.text, re.I)
        if m:
            return m.group(1)
    raise RuntimeError(f"x-csrf-token não encontrado na página de afiliados ({len(r.text)} bytes)")


def generate_links(urls, cfg, sleep=time.sleep):
    """Gera um link por vez e devolve (url, link_afiliado | None, erro | None) para cada URL."""
    require(cfg, "cookies", "tag")
    s = session(cfg)
    try:
        try:
            csrf = get_csrf(s)
        except RuntimeError:
            if not cfg.get("csrf_token"):
                raise
            csrf = cfg["csrf_token"]  # a página não deu o token: tenta o informado na tela

        def post(url):
            return s.post(LINK_API, timeout=60, json={"urls": [url], "tag": cfg["tag"]}, headers={
                "accept": "application/json, text/plain, */*", "content-type": "application/json",
                "origin": BASE, "referer": LINKBUILDER, "x-csrf-token": csrf})

        auth_errors = 0
        for i, url in enumerate(urls):
            if i:
                sleep(random.uniform(*LINK_PAUSE))
            try:
                r = post(url)
                if r.status_code in (401, 403):  # o csrf vence antes do cookie: renova e tenta 1x
                    csrf = get_csrf(s)
                    r = post(url)
                if r.status_code in (401, 403):
                    auth_errors += 1
                    if auth_errors >= MAX_AUTH_ERRORS:
                        raise RuntimeError(f"{auth_errors} erros {r.status_code} seguidos no createLink: a sessão do "
                                           "Mercado Livre caiu. Exporte os cookies de novo e salve.")
                    yield url, None, f"createLink respondeu {r.status_code}"
                    continue
                auth_errors = 0
                r.raise_for_status()
                got = (r.json().get("urls") or [{}])[0]
                yield url, got.get("short_url"), None if got.get("short_url") else f"sem link na resposta: {r.text[:200]}"
            except requests.RequestException as e:
                yield url, None, str(e)
    finally:
        save_cookies(s)
