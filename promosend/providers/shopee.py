import hashlib
import json
import time

import requests

from . import require

NAME = "Shopee"
TERM_LABEL = "Palavras-chave"
TERM_HELP = "Uma palavra-chave por linha. Cada termo vira uma busca separada."
FIELDS = [
    {"key": "app_id", "label": "App ID", "type": "text"},
    {"key": "secret", "label": "Secret", "type": "password", "secret": True},
    {"key": "min_price", "label": "Preço mínimo (R$)", "type": "number", "default": "10"},
    {"key": "min_commission", "label": "Comissão mínima (0.05 = 5%)", "type": "number", "default": "0.05"},
    {"key": "min_sales", "label": "Vendas mínimas", "type": "number", "default": "100"},
]
DEFAULT_CRONS = {"busca": "0 7-20/3 * * *", "limpeza": "0 0 * * *"}
SEARCH_DELAY = 2

API = "https://open-api.affiliate.shopee.com.br/graphql"
QUERY = ("{ productOfferV2(listType: 2, sortType: 2, limit: 50, keyword: %s) "
         "{ nodes { itemId commissionRate imageUrl price productLink offerLink productName sales } } }")


def sign(app_id, timestamp, body, secret):
    return hashlib.sha256(f"{app_id}{timestamp}{body}{secret}".encode()).hexdigest()


def search(term, cfg):
    require(cfg, "app_id", "secret")
    body = json.dumps({"query": QUERY % json.dumps(term)}, separators=(",", ":"))
    ts = str(int(time.time()))
    auth = f"SHA256 Credential={cfg['app_id']}, Timestamp={ts}, Signature={sign(cfg['app_id'], ts, body, cfg['secret'])}"
    r = requests.post(API, data=body, timeout=60,
                      headers={"Content-Type": "application/json", "Authorization": auth})
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"])

    min_price = float(cfg.get("min_price") or 0)
    min_comm = float(cfg.get("min_commission") or 0)
    min_sales = int(float(cfg.get("min_sales") or 0))
    out = []
    for n in data["data"]["productOfferV2"]["nodes"]:
        price, comm, sales = float(n["price"]), float(n["commissionRate"]), int(n["sales"])
        if price > min_price and comm >= min_comm and sales >= min_sales:
            out.append({"external_id": str(n["itemId"]), "name": n["productName"], "image": n["imageUrl"],
                        "url": n.get("productLink"), "affiliate_url": n["offerLink"],
                        "price": price, "commission": comm, "sales": sales})
    return out
