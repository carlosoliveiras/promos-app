"""Busca no Mercado Livre pela API oficial (OAuth), alternativa à raspagem da página.

A página de listagem pode cair em captcha; a API não. Exige um app do ML
(Client ID/Secret) e uma autorização da conta. O access token dura ~6 h e o
refresh token é de uso único, então cada renovação grava o novo.
"""
import base64
import hashlib
import re
import secrets
import time
from urllib.parse import unquote, urlencode, urlparse

import requests

from .. import db

AUTH_URL = "https://auth.mercadolivre.com.br/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
API = "https://api.mercadolibre.com"
KEY = "mercadolivre.api_"  # prefixo das configurações internas (tokens), fora dos campos da tela
MARGIN = 300  # renova 5 min antes de vencer
CALL_DELAY = 0.3


def _get(name):
    return db.get_setting(KEY + name)


def _set(name, value):
    db.set_setting(KEY + name, str(value))


def auth_link(cfg, redirect_uri, state):
    """Link de autorização com PKCE (S256), exigido nos apps novos do ML."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    _set("code_verifier", verifier)
    _set("redirect_uri", redirect_uri)
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": cfg["client_id"],
                                       "redirect_uri": redirect_uri, "state": state,
                                       "code_challenge": challenge, "code_challenge_method": "S256"})


def code_from(text):
    """Aceita o code puro (TG-...) ou a URL de retorno inteira colada."""
    text = (text or "").strip()
    m = re.search(r"[?&]code=([^&\s#]+)", text)
    return m.group(1) if m else (text if re.fullmatch(r"[\w-]+", text) else "")


def _token_request(data):
    r = requests.post(TOKEN_URL, data=data, timeout=30, headers={"accept": "application/json"})
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or "access_token" not in body:
        raise RuntimeError(f"OAuth do ML recusou ({r.status_code}): "
                           f"{body.get('message') or body.get('error_description') or body.get('error') or r.text[:150]}")
    _set("access_token", body["access_token"])
    if body.get("refresh_token"):
        _set("refresh_token", body["refresh_token"])
    _set("expires_at", time.time() + int(body.get("expires_in", 21600)) - MARGIN)
    if body.get("user_id"):
        _set("user_id", body["user_id"])
    _set("renewed_at", time.strftime("%Y-%m-%d %H:%M:%S"))


def authorize(cfg, code):
    if not (cfg.get("client_id") and cfg.get("client_secret")):
        raise RuntimeError("salve o Client ID e o Client Secret do app antes de autorizar")
    _token_request({"grant_type": "authorization_code", "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"], "code": code,
                    "redirect_uri": _get("redirect_uri"), "code_verifier": _get("code_verifier")})
    _set("code_verifier", "")  # de uso único
    _set("authorized_at", time.strftime("%Y-%m-%d %H:%M:%S"))


def disconnect():
    for name in ("access_token", "refresh_token", "expires_at", "user_id", "authorized_at", "renewed_at"):
        _set(name, "")


def status():
    expires = float(_get("expires_at") or 0)
    return {"authorized": bool(_get("refresh_token") or _get("access_token")), "user_id": _get("user_id"),
            "authorized_at": _get("authorized_at"), "renewed_at": _get("renewed_at"),
            "minutes_left": int((expires - time.time()) / 60) if expires else None}


def token(cfg):
    if not (_get("access_token") or _get("refresh_token")):
        raise RuntimeError("API do ML não autorizada: autorize em Provedores > Mercado Livre")
    if time.time() < float(_get("expires_at") or 0):
        return _get("access_token")
    if not _get("refresh_token"):
        raise RuntimeError("token da API do ML vencido e sem refresh token: autorize de novo")
    _token_request({"grant_type": "refresh_token", "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"], "refresh_token": _get("refresh_token")})
    return _get("access_token")


def get(path, cfg, params=None):
    r = requests.get(API + path, params=params, timeout=30,
                     headers={"Authorization": "Bearer " + token(cfg), "accept": "application/json"})
    if r.status_code in (401, 403):
        raise RuntimeError(f"API do ML respondeu {r.status_code} em {path}: {r.text[:150]}")
    r.raise_for_status()
    return r.json()


def search_text(term):
    """Texto de busca do termo: 'airfryer', ou o da URL de listagem (lista.mercadolivre.com.br/ar-condicionado)."""
    term = term.strip()
    if not term.lower().startswith("http"):
        return term
    first = unquote(urlparse(term).path.strip("/").split("/")[0]).split("_")[0]
    return first.replace("-", " ").strip()


def is_category(term):
    """Termos da fonte API são só o ID da categoria (MLB1055); o resto é URL da fonte página."""
    return bool(re.fullmatch(r"MLB\d+", term.strip(), re.I))


def resolve_category(text, cfg):
    """Na hora do cadastro: ID, URL com category=MLB..., palavra ou URL de listagem -> (ID, nome da categoria)."""
    m = re.search(r"(?:^|category=|_CategID_)(MLB\d+)", text.strip(), re.I)
    if m:
        cat_id = m.group(1).upper()
    else:
        words = search_text(text)
        if not words:
            raise RuntimeError(f"{text!r}: vitrines e cupons não existem na API; use o ID ou uma palavra-chave")
        found = get("/sites/MLB/domain_discovery/search", cfg, {"q": words, "limit": 1})
        if not found or not found[0].get("category_id"):
            raise RuntimeError(f"a API do ML não achou categoria para {words!r}; confira a grafia")
        cat_id = found[0]["category_id"]
    r = requests.get(f"{API}/categories/{cat_id}", timeout=30)  # público, sem token
    if r.status_code == 404:
        raise RuntimeError(f"a categoria {cat_id} não existe no Mercado Livre")
    r.raise_for_status()
    return cat_id, r.json().get("name") or cat_id


def search(term, cfg, sleep=time.sleep):
    """Mais vendidos da categoria; de cada produto, a oferta mais barata com desconto (> 3%)."""
    if not is_category(term):
        raise RuntimeError("na fonte API o termo precisa ser o ID da categoria (ex.: MLB1055); "
                           "cadastre em Provedores > Mercado Livre > Categorias da API")
    highlights = get(f"/highlights/MLB/category/{term.strip().upper()}", cfg)
    out = []
    for pid in [x["id"] for x in highlights.get("content", []) if x.get("type") == "PRODUCT"]:
        sleep(CALL_DELAY)
        try:
            items = get(f"/products/{pid}/items", cfg, {"limit": 20}).get("results", [])
        except requests.RequestException:
            continue
        offers = [x for x in items if x.get("price") and x.get("original_price")
                  and float(x["original_price"]) > float(x["price"]) * 1.03]
        if not offers:
            continue
        best = min(offers, key=lambda x: float(x["price"]))
        try:
            product = get(f"/products/{pid}", cfg)
        except requests.RequestException:
            continue
        pictures = product.get("pictures") or []
        price, original = float(best["price"]), float(best["original_price"])
        num = str(best["item_id"]).upper().replace("MLB", "")
        out.append({"external_id": f"MLB{num}", "name": product.get("name"),
                    "image": ((pictures[0].get("url") if pictures else "") or "").replace("http://", "https://"),
                    "url": f"https://produto.mercadolivre.com.br/MLB-{num}",
                    "price": price, "price_original": original,
                    "discount": f"{round((1 - price / original) * 100)}% OFF"})
    return out
