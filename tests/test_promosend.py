import json
import re

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    from promosend import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    from promosend.app import create_app
    from promosend.scheduler import seed_jobs
    a = create_app()
    seed_jobs()
    a.config["TESTING"] = True
    return a


def csrf(client, path):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


POLYCARD = """
<div class="poly-card poly-card--grid">
  <img class="poly-component__picture" src="data:image/gif;base64,xx" data-src="https://img/1.jpg"/>
  <a class="poly-component__title" href="https://www.mercadolivre.com.br/fone/p/MLB-123?x=1#frag">Fone &amp; Bluetooth</a>
  <s class="andes-money-amount andes-money-amount--previous"><span data-andes-money-amount-fraction="true">1.299</span></s>
  <div class="poly-price__current"><span data-andes-money-amount-fraction="true">999</span><span data-andes-money-amount-cents="true">90</span></div>
  <span class="poly-price__disc_label">23% OFF</span>
</div>
<div class="poly-card">
  <img class="poly-component__picture" src="https://img/2.jpg"/>
  <a class="poly-component__title" href="https://click1.mercadolivre.com.br/ad">Anúncio</a>
  <div class="poly-price__current"><span data-andes-money-amount-fraction="true">10</span></div>
</div>
"""

CAROUSEL = """
<div class="dynamic-carousel__item-container"><a class="splinter-link" href="https://produto.mercadolivre.com.br/MLB-555-x">
<img alt="" src="https://img/3.jpg"><span class="dynamic-carousel__oldprice">R$ 200</span>
<span class="dynamic-carousel__price"><span>R$ 150</span><sup class="dynamic-carousel__price-decimals">50</sup></span>
<sup class="dynamic-carousel__discount">25% OFF</sup></a><h3 class="dynamic-carousel__title">Tênis</h3></div>
"""


def test_ml_polycard():
    from promosend.providers import mercadolivre
    [p] = mercadolivre.parse(POLYCARD)
    assert p["external_id"] == "MLB123"
    assert p["url"] == "https://www.mercadolivre.com.br/fone/p/MLB-123"
    assert p["name"] == "Fone & Bluetooth" and p["image"] == "https://img/1.jpg"
    assert p["price"] == 999.90 and p["price_original"] == 1299 and p["discount"] == "23% OFF"


def test_ml_carousel():
    from promosend.providers import mercadolivre
    [p] = mercadolivre.parse(CAROUSEL)
    assert (p["external_id"], p["price"], p["price_original"], p["name"]) == ("MLB555", 150.5, 200, "Tênis")


def test_shopee_search_signs_and_filters(monkeypatch):
    from promosend.providers import shopee
    sent = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"productOfferV2": {"nodes": [
                {"itemId": 1, "productName": "Bom", "imageUrl": "i", "price": "50.0", "commissionRate": "0.1",
                 "sales": 500, "offerLink": "https://s.shopee/1", "productLink": "p"},
                {"itemId": 2, "productName": "Pouca venda", "imageUrl": "i", "price": "50.0", "commissionRate": "0.1",
                 "sales": 3, "offerLink": "o", "productLink": "p"}]}}}

    def post(url, data, headers, timeout):
        sent.update(body=data, auth=headers["Authorization"])
        return Resp()

    monkeypatch.setattr(shopee.requests, "post", post)
    cfg = {"app_id": "1", "secret": "s", "min_price": "10", "min_commission": "0.05", "min_sales": "100"}
    [p] = shopee.search('fone "bt"', cfg)
    assert p["external_id"] == "1" and p["affiliate_url"] == "https://s.shopee/1"
    assert '\\"fone \\\\\\"bt\\\\\\"\\"' in sent["body"]  # termo escapado dentro da query GraphQL
    ts = re.search(r"Timestamp=(\d+)", sent["auth"]).group(1)
    assert sent["auth"].endswith("Signature=" + shopee.sign("1", ts, sent["body"], "s"))
    json.loads(sent["body"])


def test_upsert_keeps_status_and_link(app):
    from promosend import db
    db.upsert_products("mercadolivre", "Geral", [{"external_id": "MLB1", "name": "a", "image": "i", "url": "u", "price": 1}])
    db.execute("UPDATE products SET affiliate_url = 'aff', status = 'ENVIADO'")
    db.upsert_products("mercadolivre", "Geral", [{"external_id": "MLB1", "name": "b", "image": "i", "url": "u", "price": 2}])
    r = db.row("SELECT * FROM products")
    assert (r["name"], r["price"], r["affiliate_url"], r["status"]) == ("b", 2, "aff", "ENVIADO")


def test_links_flow(app, monkeypatch):
    from promosend import db, flows
    from promosend.providers import mercadolivre
    db.upsert_products("mercadolivre", "", [{"external_id": "MLB1", "name": "a", "image": "i", "url": "u1", "price": 1}])
    monkeypatch.setattr(mercadolivre, "generate_links", lambda urls, cfg: {u: "short-" + u for u in urls})
    assert flows.links("mercadolivre") == "1 de 1 links gerados"
    assert db.row("SELECT status, affiliate_url FROM products")["affiliate_url"] == "short-u1"


def login(app):
    c = app.test_client()
    token = csrf(c, "/setup")
    c.post("/setup", data={"username": "adm", "password": "12345678", "confirm": "12345678", "csrf": token})
    return c, csrf(c, "/config/whatsapp")


def test_whatsapp_bots_and_groups(app, monkeypatch):
    from promosend import db, whatsapp
    c, token = login(app)
    c.post("/config/whatsapp", data={"url": "http://wuz:8080/", "csrf": token})
    c.post("/config/whatsapp/bots", data={"name": "Bot 1", "token": "tok123", "csrf": token})
    bid = db.row("SELECT id FROM bots")["id"]

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"Groups": [{"JID": "2@g.us", "Name": "Zeta"}, {"JID": "1@g.us", "Name": "Alfa | ofertas"}]}}

    calls = []
    monkeypatch.setattr(whatsapp.requests, "get", lambda url, headers, timeout: calls.append((url, headers)) or Resp())
    page = c.get(f"/config/whatsapp/bots/{bid}?listar=1").get_data(as_text=True)
    assert calls == [("http://wuz:8080/group/list", {"Token": "tok123"})]
    assert page.index("Alfa") < page.index("Zeta")

    c.post(f"/config/whatsapp/bots/{bid}", data={"name": "Bot 1", "token": "", "groups_form": "1",
                                                 "group": ["1@g.us|Alfa | ofertas"], "csrf": token})
    assert tuple(db.row("SELECT jid, name FROM bot_groups")) == ("1@g.us", "Alfa | ofertas")
    assert db.row("SELECT token FROM bots")["token"] == "tok123"  # token em branco mantém
    assert "tok123" not in c.get("/config/whatsapp").get_data(as_text=True)
    c.post(f"/config/whatsapp/bots/{bid}/excluir", data={"csrf": token})
    assert not db.rows("SELECT * FROM bot_groups")


def test_postagem(app, monkeypatch):
    from promosend import db, flows, whatsapp
    db.set_setting("whatsapp.url", "http://wuz")
    db.set_setting("whatsapp.post_count", "2")
    for bid, posts in [(1, 1), (2, 0)]:
        db.execute("INSERT INTO bots (id, name, token, posts) VALUES (?, ?, ?, ?)", bid, f"b{bid}", f"t{bid}", posts)
        db.execute("INSERT INTO bot_groups VALUES (?, ?, ?)", bid, f"g{bid}a@g.us", "A")
    db.execute("INSERT INTO bot_groups VALUES (1, 'g1b@g.us', 'B')")
    db.upsert_products("shopee", "", [{"external_id": str(i), "name": f"P{i}", "image": "img", "price": 10.5,
                                       "affiliate_url": f"https://aff/{i}"} for i in range(3)])
    sent, sleeps = [], []
    monkeypatch.setattr(whatsapp, "image_data_url", lambda url: "data:image/jpeg;base64,AA")
    monkeypatch.setattr(whatsapp, "send_image", lambda url, token, jid, image, cap: sent.append((token, jid, cap)))

    assert flows.postagem(sleep=sleeps.append) == "2 produtos, 4 postagens"
    assert {(t, j) for t, j, _ in sent} == {("t1", "g1a@g.us"), ("t1", "g1b@g.us")}  # bot 2 não posta
    assert len(sleeps) == 3 and all(1 <= s <= 5 for s in sleeps)
    assert "*Por R$ 10,50*" in sent[0][2] and "https://aff/" in sent[0][2]
    assert db.row("SELECT COUNT(*) AS n FROM products WHERE status = 'ENVIADO'")["n"] == 2


def test_setup_login_and_pages(app):
    c = app.test_client()
    assert c.get("/").headers["Location"].endswith("/setup")
    token = csrf(c, "/setup")
    assert c.post("/setup", data={"username": "adm", "password": "x", "confirm": "x", "csrf": token}).status_code == 200
    r = c.post("/setup", data={"username": "adm", "password": "12345678", "confirm": "12345678", "csrf": token})
    assert r.status_code == 302
    for path in ["/", "/config/provedores", "/config/provedores/shopee", "/config/provedores/mercadolivre", "/config/agendamentos"]:
        assert c.get(path).status_code == 200, path

    token = csrf(c, "/config/provedores/shopee")
    c.post("/config/provedores/shopee", data={"app_id": "9", "secret": "abc", "min_price": "5", "csrf": token})
    c.post("/config/provedores/shopee", data={"app_id": "9", "secret": "", "min_price": "5", "csrf": token})
    from promosend import db
    assert db.get_setting("shopee.secret") == "abc"  # secret em branco não apaga
    c.post("/config/provedores/shopee/termos", data={"terms": "fone\n\nmouse\nfone", "csrf": token})
    assert [t["term"] for t in db.rows("SELECT term FROM terms")] == ["fone", "mouse"]

    c.post("/config/agendamentos/shopee.busca", data={"cron": "bad", "enabled": "on", "csrf": token})
    assert db.row("SELECT cron FROM jobs WHERE id = 'shopee.busca'")["cron"] == "0 7-20/3 * * *"
    c.post("/config/agendamentos/shopee.busca", data={"cron": "*/5 * * * *", "enabled": "on", "csrf": token})
    assert tuple(db.row("SELECT cron, enabled FROM jobs WHERE id = 'shopee.busca'")) == ("*/5 * * * *", 1)

    assert c.post("/config/agendamentos/shopee.busca", data={"cron": "* * * * *"}).status_code == 400  # sem CSRF
    c.post("/logout", data={"csrf": token})
    assert c.get("/").headers["Location"].endswith("/login")
    assert c.get("/setup").headers["Location"].endswith("/login")  # setup só na primeira instalação
