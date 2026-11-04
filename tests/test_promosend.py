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


def test_ml_search_detects_captcha(monkeypatch):
    from promosend.providers import mercadolivre

    class Resp:
        status_code, text = 200, "<html>captcha</html>"
        url = "https://www.mercadolivre.com.br/captcha/wall/logged?go_url=x"
        def raise_for_status(self): pass

    class FakeSession:
        headers, cookies = {}, mercadolivre.requests.cookies.RequestsCookieJar()
        def get(self, url, timeout): return Resp()

    monkeypatch.setattr(mercadolivre.requests, "Session", FakeSession)
    with pytest.raises(RuntimeError, match="anti-robô"):
        mercadolivre.search("https://lista.mercadolivre.com.br/celular", {"cookies": "a=1"})
    Resp.url, Resp.text = "https://lista.mercadolivre.com.br/celular", POLYCARD
    assert len(mercadolivre.search("https://lista.mercadolivre.com.br/celular", {})) == 1


def test_ml_cookie_header():
    from promosend.providers.mercadolivre import cookie_header
    exported = '[{"name": "ssid", "value": "abc", "domain": ".mercadolivre.com.br"}, {"name": "cp", "value": "6321"}]'
    assert cookie_header(exported) == "ssid=abc; cp=6321"
    assert cookie_header(" ssid=abc; cp=6321 ") == "ssid=abc; cp=6321"
    assert cookie_header(None) == ""


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
    monkeypatch.setattr(mercadolivre, "generate_links", lambda urls, cfg: ((u, "short-" + u, None) for u in urls))
    assert flows.links("mercadolivre") == "1 de 1 links gerados"
    assert db.row("SELECT status, affiliate_url FROM products")["affiliate_url"] == "short-u1"


def login(app):
    c = app.test_client()
    token = csrf(c, "/setup")
    c.post("/setup", data={"username": "adm", "password": "12345678", "confirm": "12345678", "csrf": token})
    return c, csrf(c, "/config/whatsapp")


class FakeWuzapi:
    """Simula as respostas do wuzapi por caminho e guarda as chamadas."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, timeout, json=None):
        path = "/" + url.split("/", 3)[3]
        self.calls.append((method, path, headers, json))
        status, body = self.routes.get((method, path), (404, {"success": False, "error": "nao mapeado"}))

        class Resp:
            ok = status < 400
            status_code = status
            def json(self): return body
        return Resp()


def test_whatsapp_bots_and_groups(app, monkeypatch):
    from promosend import db, whatsapp
    c, token = login(app)
    c.post("/config/whatsapp", data={"url": "http://wuz:8080/", "csrf": token})
    c.post("/config/whatsapp/bots", data={"name": "Bot 1", "token": "tok123", "csrf": token})
    bid = db.row("SELECT id FROM bots")["id"]

    wuz = FakeWuzapi({
        ("GET", "/session/status"): (200, {"success": True, "data": {"connected": True, "loggedIn": True}}),
        ("GET", "/group/list"): (200, {"success": True, "data": {"Groups": [
            {"JID": "2@g.us", "Name": "Zeta"}, {"JID": "1@g.us", "Name": "Alfa | ofertas"}]}}),
    })
    monkeypatch.setattr(whatsapp.requests, "request", wuz)
    page = c.get(f"/config/whatsapp/bots/{bid}?listar=1").get_data(as_text=True)
    assert ("GET", "/group/list", {"Token": "tok123"}, None) in wuz.calls
    assert page.index("Alfa") < page.index("Zeta") and "Conectado" in page

    c.post(f"/config/whatsapp/bots/{bid}", data={"name": "Bot 1", "token": "", "groups_form": "1",
                                                 "group": ["1@g.us|Alfa | ofertas"], "csrf": token})
    assert tuple(db.row("SELECT jid, name FROM bot_groups")) == ("1@g.us", "Alfa | ofertas")
    assert db.row("SELECT token FROM bots")["token"] == "tok123"  # token em branco mantém
    assert "tok123" not in c.get("/config/whatsapp").get_data(as_text=True)
    c.post(f"/config/whatsapp/bots/{bid}/excluir", data={"csrf": token})
    assert not db.rows("SELECT * FROM bot_groups")


def test_whatsapp_auto_create_and_qr(app, monkeypatch):
    from promosend import db, whatsapp
    c, token = login(app)
    # sem token de admin, bot sem token é recusado
    c.post("/config/whatsapp", data={"url": "http://wuz:8080", "csrf": token})
    c.post("/config/whatsapp/bots", data={"name": "Novo", "token": "", "csrf": token})
    assert not db.rows("SELECT * FROM bots")

    qr_png = "data:image/png;base64,iVBORw0KGgo="
    wuz = FakeWuzapi({
        ("POST", "/admin/users"): (200, {"success": True, "data": {"id": "x"}}),
        ("GET", "/session/status"): (200, {"success": True, "data": {"connected": False, "loggedIn": False}}),
        ("POST", "/session/connect"): (200, {"success": True, "data": {"details": "Connected!"}}),
        ("GET", "/session/qr"): (200, {"success": True, "data": {"QRCode": qr_png}}),
    })
    monkeypatch.setattr(whatsapp.requests, "request", wuz)
    c.post("/config/whatsapp", data={"url": "http://wuz:8080", "admin_token": "ADM", "csrf": token})
    r = c.post("/config/whatsapp/bots", data={"name": "Novo", "token": "", "csrf": token})
    b = db.row("SELECT * FROM bots")
    assert r.headers["Location"].endswith(f"/config/whatsapp/bots/{b['id']}")
    [(method, path, headers, body)] = [x for x in wuz.calls if x[1] == "/admin/users"]
    assert headers == {"Authorization": "ADM"} and body == {"name": "Novo", "token": b["token"]} and len(b["token"]) == 32

    r = c.post(f"/config/whatsapp/bots/{b['id']}/conectar", data={"csrf": token})
    assert r.headers["Location"].endswith("?qr=1")
    assert any(x[1] == "/session/connect" for x in wuz.calls)
    wuz.routes[("GET", "/session/status")] = (200, {"success": True, "data": {"connected": True, "loggedIn": False}})
    page = c.get(f"/config/whatsapp/bots/{b['id']}?qr=1").get_data(as_text=True)
    assert qr_png in page and 'http-equiv="refresh"' in page

    wuz.routes[("GET", "/session/status")] = (200, {"success": True, "data": {"connected": True, "loggedIn": True}})
    page = c.get(f"/config/whatsapp/bots/{b['id']}?qr=1").get_data(as_text=True)
    assert "Conectado" in page and 'http-equiv="refresh"' not in page

    # erro do wuzapi aparece para o usuário
    wuz.routes[("POST", "/session/logout")] = (500, {"success": False, "error": "not logged in"})
    c.post(f"/config/whatsapp/bots/{b['id']}/desconectar", data={"csrf": token})
    assert "not logged in" in c.get(f"/config/whatsapp/bots/{b['id']}").get_data(as_text=True)


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


def test_logs_run_job_and_retention(app, monkeypatch):
    from promosend import db, scheduler
    from promosend.providers import shopee
    db.execute("INSERT INTO terms (provider, term) VALUES ('shopee', 'fone'), ('shopee', 'mouse')")
    monkeypatch.setattr(shopee, "SEARCH_DELAY", 0)

    def search(term, cfg):
        if term == "mouse":
            raise RuntimeError("API fora do ar")
        return [{"external_id": "1", "name": "Fone", "image": "i", "price": 20, "affiliate_url": "a"}]
    monkeypatch.setattr(shopee, "search", search)

    scheduler.run_job("shopee.busca", "manual")
    logs = db.rows("SELECT * FROM logs ORDER BY id")
    assert [(l["level"], l["message"].split(":")[0]) for l in logs] == [
        ("info", "Termo 'fone'"), ("erro", "Termo 'mouse'"), ("aviso", "Execução manual em 0.0s")]
    assert "RuntimeError: API fora do ar" in logs[1]["details"]
    assert "1 com erro" in logs[2]["message"]

    from promosend import flows
    monkeypatch.setitem(flows.FLOWS, "busca", lambda pkey: 1 / 0)
    scheduler.run_job("shopee.busca")
    last = db.row("SELECT * FROM logs ORDER BY id DESC")
    assert last["level"] == "erro" and "ZeroDivisionError" in last["details"] and "agendado" in last["message"]

    db.execute("INSERT INTO logs (created_at, job, level, message) VALUES (datetime('now', 'localtime', '-8 days'), 'x', 'info', 'velho')")
    db.prune_logs()
    assert not db.rows("SELECT * FROM logs WHERE message = 'velho'") and db.rows("SELECT * FROM logs")

    c, _ = login(app)
    page = c.get("/logs?level=erro").get_data(as_text=True)
    assert "Shopee · Busca de produtos" in page and "API fora do ar" in page and "Termo &#39;fone&#39;" not in page


def test_ml_api_search_picks_discounted_offer(app, monkeypatch):
    from promosend.providers import mercadolivre, ml_api
    assert ml_api.category_of("MLB1055") == ml_api.category_of("https://x/ofertas?category=mlb1055") == "MLB1055"
    with pytest.raises(RuntimeError, match="ID da categoria"):
        ml_api.category_of("https://lista.mercadolivre.com.br/celular")

    responses = {
        "/highlights/MLB/category/MLB1055": {"content": [{"id": "P1", "type": "PRODUCT"}, {"id": "P2", "type": "PRODUCT"},
                                                         {"id": "I9", "type": "ITEM"}]},
        "/products/P1/items": {"results": [{"item_id": "MLB11", "price": 100, "original_price": 150},
                                           {"item_id": "MLB12", "price": 90, "original_price": 120},
                                           {"item_id": "MLB13", "price": 80, "original_price": 81}]},  # < 3%: ignora
        "/products/P2/items": {"results": [{"item_id": "MLB21", "price": 50}]},  # sem desconto: ignora o produto
        "/products/P1": {"name": "Celular X", "pictures": [{"url": "http://img/1.jpg"}]},
    }
    monkeypatch.setattr(ml_api, "get", lambda path, cfg, params=None: responses[path])
    monkeypatch.setattr(ml_api, "CALL_DELAY", 0)
    [p] = mercadolivre.search("MLB1055", {"fonte": "api"})
    assert p == {"external_id": "MLB12", "name": "Celular X", "image": "https://img/1.jpg",
                 "url": "https://produto.mercadolivre.com.br/MLB-12", "price": 90, "price_original": 120,
                 "discount": "25% OFF"}


def test_ml_api_oauth_flow_and_refresh(app, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    from promosend import db
    from promosend.providers import ml_api
    c, token = login(app)
    c.post("/config/provedores/mercadolivre", data={"fonte": "api", "client_id": "CID", "client_secret": "SEC",
                                                    "redirect_uri": "", "csrf": token})
    assert db.get_setting("mercadolivre.fonte") == "api"

    r = c.post("/config/provedores/mercadolivre/api/autorizar", data={"csrf": token})
    q = parse_qs(urlparse(r.headers["Location"]).query)
    assert r.headers["Location"].startswith(ml_api.AUTH_URL) and q["client_id"] == ["CID"]
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].endswith("/config/provedores/mercadolivre/api/retorno")
    r = c.post("/config/provedores/mercadolivre/api/autorizar", data={"csrf": token},
               headers={"X-Forwarded-Host": "promo.ngrok-free.dev"})  # como chega pelo ngrok: http, sem proto
    assert parse_qs(urlparse(r.headers["Location"]).query)["redirect_uri"] == [
        "https://promo.ngrok-free.dev/config/provedores/mercadolivre/api/retorno"]  # fora do localhost, sempre https

    posts = []

    class Resp:
        status_code = 200
        def __init__(self, body): self.body = body
        def json(self): return self.body

    def post(url, data, timeout, headers):
        posts.append(data)
        n = len(posts)
        return Resp({"access_token": f"AT{n}", "refresh_token": f"RT{n}", "expires_in": 21600, "user_id": 42})
    monkeypatch.setattr(ml_api.requests, "post", post)

    c.get("/config/provedores/mercadolivre/api/retorno?code=TG-1&state=errado")
    assert not posts  # state diferente não troca o code (e invalida o state pendente)
    r = c.post("/config/provedores/mercadolivre/api/autorizar", data={"csrf": token})
    state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    c.get(f"/config/provedores/mercadolivre/api/retorno?code=TG-1&state={state}")
    assert posts[0]["grant_type"] == "authorization_code" and posts[0]["code"] == "TG-1" and posts[0]["code_verifier"]
    assert "Autorizada (conta 42)" in c.get("/config/provedores/mercadolivre").get_data(as_text=True)

    cfg = {"client_id": "CID", "client_secret": "SEC"}
    assert ml_api.token(cfg) == "AT1" and len(posts) == 1  # ainda válido
    db.set_setting("mercadolivre.api_expires_at", "0")
    assert ml_api.token(cfg) == "AT2" and posts[1] == {"grant_type": "refresh_token", "client_id": "CID",
                                                        "client_secret": "SEC", "refresh_token": "RT1"}
    assert db.get_setting("mercadolivre.api_refresh_token") == "RT2"  # refresh é de uso único: guarda o novo

    assert ml_api.code_from("https://example.com/retorno?code=TG-abc&state=x") == "TG-abc"
    c.post("/config/provedores/mercadolivre/api/desconectar", data={"csrf": token})
    assert not ml_api.status()["authorized"]


def test_ml_cookie_header_from_curl():
    from promosend.providers.mercadolivre import cookie_header
    curl = "curl 'https://www.mercadolivre.com.br/afiliados' \\\n  -H 'accept: */*' \\\n  -H 'cookie: ssid=abc; orguseridp=1' \\\n  -H 'x: y'"
    assert cookie_header(curl) == "ssid=abc; orguseridp=1"
    assert cookie_header("curl 'https://x' -b 'ssid=1; cp=2'") == "ssid=1; cp=2"


def test_ml_generate_links_session_csrf_and_cookie_refresh(app, monkeypatch):
    from promosend import db
    from promosend.providers import mercadolivre as ml

    class Resp:
        def __init__(self, status=200, body=None, text="", url=""):
            self.status_code, self.body, self.text, self.url = status, body, text, url
        def raise_for_status(self):
            if self.status_code >= 400:
                raise ml.requests.HTTPError(str(self.status_code))
        def json(self): return self.body

    class FakeSession:
        def __init__(self):
            self.headers, self.cookies, self.csrf_calls, self.posts = {}, ml.requests.cookies.RequestsCookieJar(), 0, []
        def get(self, url, timeout):
            self.csrf_calls += 1
            self.cookies.set("ssid", f"novo{self.csrf_calls}", domain=".mercadolivre.com.br")  # Set-Cookie do ML
            return Resp(text=f'<meta name="csrf-token" content="CSRF{self.csrf_calls}">', url=url)
        def post(self, url, timeout, json, headers):
            self.posts.append((json["urls"][0], headers["x-csrf-token"]))
            if json["urls"][0] == "u2" and headers["x-csrf-token"] == "CSRF1":
                return Resp(401)  # csrf vencido: deve renovar e tentar de novo
            if json["urls"][0] == "u3":
                return Resp(200, {"urls": [{"origin_url": "u3"}]}, text="sem short")
            return Resp(200, {"urls": [{"origin_url": json["urls"][0], "short_url": "s-" + json["urls"][0]}]})

    fake = FakeSession()
    monkeypatch.setattr(ml.requests, "Session", lambda: fake)
    sleeps = []
    cfg = {"cookies": "ssid=velho; orguseridp=9", "tag": "minhatag"}
    out = list(ml.generate_links(["u1", "u2", "u3"], cfg, sleep=sleeps.append))
    assert [(u, s) for u, s, _ in out] == [("u1", "s-u1"), ("u2", "s-u2"), ("u3", None)]
    assert "sem link" in out[2][2]
    assert fake.posts == [("u1", "CSRF1"), ("u2", "CSRF1"), ("u2", "CSRF2"), ("u3", "CSRF2")]
    assert len(sleeps) == 2 and all(5 <= x <= 14 for x in sleeps)
    saved = db.get_setting("mercadolivre.cookies")
    assert "ssid=novo2" in saved and "orguseridp=9" in saved  # cookies renovados pelo ML são guardados

    # 5 erros de autenticação seguidos param o fluxo com mensagem clara
    fake2 = FakeSession()
    fake2.post = lambda url, timeout, json, headers: Resp(403)
    monkeypatch.setattr(ml.requests, "Session", lambda: fake2)
    with pytest.raises(RuntimeError, match="sessão do Mercado Livre caiu"):
        list(ml.generate_links([f"u{i}" for i in range(10)], cfg, sleep=lambda s: None))

    # página de afiliados caindo no captcha vira erro explicado
    fake3 = FakeSession()
    fake3.get = lambda url, timeout: Resp(url="https://www.mercadolivre.com.br/captcha/wall/logged?x=1")
    monkeypatch.setattr(ml.requests, "Session", lambda: fake3)
    with pytest.raises(RuntimeError, match="anti-robô"):
        list(ml.generate_links(["u1"], cfg))
