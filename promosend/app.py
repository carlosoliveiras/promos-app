import secrets

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, scheduler, whatsapp
from .flows import FLOW_LABELS, POST_JOB, brl, default_jobs, job_label
from .providers import PROVIDERS, ml_api

PUBLIC = {"login", "setup", "static"}
PAGE_SIZE = 24
LOG_PAGE = 50


def create_app():
    db.init()
    app = Flask(__name__)
    # o app só escuta em 127.0.0.1; o proxy (ngrok) informa https e o domínio público
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    if not db.get_setting("secret_key"):
        db.set_setting("secret_key", secrets.token_hex(32))
    app.secret_key = db.get_setting("secret_key")
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, PROVIDERS=PROVIDERS, FLOW_LABELS=FLOW_LABELS, job_label=job_label)
    app.jinja_env.filters["brl"] = lambda v: "" if v is None else brl(v)

    @app.before_request
    def guard():
        if request.method == "POST" and request.form.get("csrf") != session.get("csrf"):
            abort(400, "token CSRF inválido")
        if request.endpoint in PUBLIC:
            return
        if not db.row("SELECT 1 FROM users"):
            return redirect(url_for("setup"))
        if "user" not in session:
            return redirect(url_for("login"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if db.row("SELECT 1 FROM users"):
            return redirect(url_for("login"))
        if request.method == "POST":
            user, pwd = request.form["username"].strip(), request.form["password"]
            if not user or len(pwd) < 8:
                flash("Informe um usuário e uma senha com pelo menos 8 caracteres.")
            elif pwd != request.form["confirm"]:
                flash("As senhas não conferem.")
            else:
                db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                           user, generate_password_hash(pwd))
                session.clear()
                session["user"] = user
                return redirect(url_for("providers"))
        return render_template("auth.html", setup=True)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not db.row("SELECT 1 FROM users"):
            return redirect(url_for("setup"))
        if request.method == "POST":
            u = db.row("SELECT * FROM users WHERE username = ?", request.form["username"].strip())
            if u and check_password_hash(u["password_hash"], request.form["password"]):
                session.clear()
                session["user"] = u["username"]
                return redirect(url_for("products"))
            flash("Usuário ou senha inválidos.")
        return render_template("auth.html", setup=False)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def products():
        provider, status = request.args.get("provider", ""), request.args.get("status", "")
        page = max(request.args.get("page", 1, type=int), 1)
        where = "WHERE (? = '' OR provider = ?) AND (? = '' OR status = ?)"
        args = (provider, provider, status, status)
        total = db.row(f"SELECT COUNT(*) AS n FROM products {where}", *args)["n"]
        items = db.rows(f"SELECT * FROM products {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                        *args, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        return render_template("products.html", items=items, provider=provider, status=status,
                               page=page, pages=max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1), total=total)

    @app.route("/logs")
    def logs():
        job, level = request.args.get("job", ""), request.args.get("level", "")
        page = max(request.args.get("page", 1, type=int), 1)
        where, args = "WHERE (? = '' OR job = ?) AND (? = '' OR level = ?)", (job, job, level, level)
        total = db.row(f"SELECT COUNT(*) AS n FROM logs {where}", *args)["n"]
        items = db.rows(f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ? OFFSET ?", *args, LOG_PAGE, (page - 1) * LOG_PAGE)
        jobs = [r["job"] for r in db.rows("SELECT DISTINCT job FROM logs ORDER BY job")]
        return render_template("logs.html", items=items, job=job, level=level, jobs=jobs, levels=db.LOG_LEVELS,
                               page=page, pages=max((total + LOG_PAGE - 1) // LOG_PAGE, 1), total=total, days=db.LOG_DAYS)

    @app.route("/config")
    def config():
        return redirect(url_for("providers"))

    @app.route("/config/provedores")
    def providers():
        return render_template("providers.html")

    def get_provider(pkey):
        if pkey not in PROVIDERS:
            abort(404)
        return PROVIDERS[pkey]

    @app.route("/config/provedores/<pkey>", methods=["GET", "POST"])
    def provider(pkey):
        prov = get_provider(pkey)
        if request.method == "POST":
            for f in prov.FIELDS:
                value = request.form.get(f["key"], "").strip()
                if f.get("secret") and not value:
                    continue  # campo secreto em branco mantém o valor salvo
                db.set_setting(f"{pkey}.{f['key']}", value)
            flash("Configuração salva.")
            return redirect(url_for("provider", pkey=pkey))
        cfg = db.provider_config(pkey, prov.FIELDS)
        terms = db.rows("SELECT * FROM terms WHERE provider = ? ORDER BY id", pkey)
        extra = {}
        if pkey == "mercadolivre":
            extra = {"api": ml_api.status(), "callback": ml_callback_url(),
                     "api_terms": [t for t in terms if ml_api.is_category(t["term"])]}
            terms = [t for t in terms if not ml_api.is_category(t["term"])]
        return render_template("provider.html", pkey=pkey, prov=prov, cfg=cfg, terms=terms, **extra)

    def ml_callback_url():
        # o ML só aceita https; atrás do ngrok o pedido chega como http e sem X-Forwarded-Proto
        local = request.host.split(":")[0] in ("localhost", "127.0.0.1", "[::1]")
        return url_for("ml_api_callback", _external=True, _scheme="http" if local else "https")

    def ml_cfg():
        return db.provider_config("mercadolivre", PROVIDERS["mercadolivre"].FIELDS)

    def ml_authorize(code):
        try:
            ml_api.authorize(ml_cfg(), code)
            flash("API do Mercado Livre autorizada. O token renova sozinho.")
        except RuntimeError as e:
            flash(f"Não foi possível autorizar: {e}")
        return redirect(url_for("provider", pkey="mercadolivre"))

    @app.post("/config/provedores/mercadolivre/api/autorizar")
    def ml_api_start():
        cfg = ml_cfg()
        if not (cfg["client_id"] and cfg["client_secret"]):
            flash("Salve o Client ID e o Client Secret do app antes de autorizar.")
            return redirect(url_for("provider", pkey="mercadolivre"))
        session["ml_oauth_state"] = state = secrets.token_urlsafe(16)
        return redirect(ml_api.auth_link(cfg, cfg["redirect_uri"] or ml_callback_url(), state))

    @app.route("/config/provedores/mercadolivre/api/retorno")
    def ml_api_callback():
        if request.args.get("error"):
            flash(f"O Mercado Livre não autorizou: {request.args.get('error_description') or request.args['error']}")
            return redirect(url_for("provider", pkey="mercadolivre"))
        if not request.args.get("state") or request.args["state"] != session.pop("ml_oauth_state", None):
            flash("Retorno de autorização inválido ou expirado. Clique em Autorizar de novo.")
            return redirect(url_for("provider", pkey="mercadolivre"))
        return ml_authorize(request.args.get("code", ""))

    @app.post("/config/provedores/mercadolivre/api/codigo")
    def ml_api_code():
        code = ml_api.code_from(request.form.get("url"))
        if not code:
            flash("Não achei o code=... na URL colada.")
            return redirect(url_for("provider", pkey="mercadolivre"))
        return ml_authorize(code)

    @app.post("/config/provedores/mercadolivre/api/categorias")
    def ml_api_categories():
        cfg, grupo = ml_cfg(), request.form.get("grupo", "").strip()
        added, errors = 0, []
        for line in [x.strip() for x in request.form["categorias"].splitlines() if x.strip()]:
            try:
                cat_id, name = ml_api.resolve_category(line, cfg)
            except Exception as e:
                errors.append(str(e))
                continue
            added += db.execute("INSERT OR IGNORE INTO terms (provider, term, grupo) VALUES ('mercadolivre', ?, ?)",
                                cat_id, grupo or name)
        flash(f"{added} categoria(s) adicionada(s)." + (" Erros: " + " | ".join(errors) if errors else ""))
        if "voltar" in request.form:  # veio da tela de navegação: continua no mesmo nível
            return redirect(url_for("ml_api_browse", id=request.form["voltar"] or None))
        return redirect(url_for("provider", pkey="mercadolivre"))

    @app.get("/config/provedores/mercadolivre/api/categorias")
    def ml_api_browse():
        cat_id = request.args.get("id", "").strip().upper()
        if cat_id and not ml_api.is_category(cat_id):
            abort(404)
        current, path, children, error = None, [], [], None
        try:
            current, path, children = ml_api.category_tree(cat_id, ml_cfg())
        except Exception as e:
            error = str(e)
        registered = {t["term"] for t in db.rows("SELECT term FROM terms WHERE provider = 'mercadolivre'")}
        return render_template("ml_categories.html", current=current, path=path, children=children,
                               error=error, registered=registered, cat_id=cat_id)

    @app.post("/config/provedores/mercadolivre/api/desconectar")
    def ml_api_disconnect():
        ml_api.disconnect()
        flash("API do Mercado Livre desconectada.")
        return redirect(url_for("provider", pkey="mercadolivre"))

    @app.post("/config/provedores/<pkey>/termos")
    def add_terms(pkey):
        get_provider(pkey)
        grupo = request.form.get("grupo", "").strip()
        new = [t.strip() for t in request.form["terms"].splitlines() if t.strip()]
        for t in new:
            db.execute("INSERT OR IGNORE INTO terms (provider, term, grupo) VALUES (?, ?, ?)", pkey, t, grupo)
        flash(f"{len(new)} termo(s) adicionado(s).")
        return redirect(url_for("provider", pkey=pkey))

    @app.post("/config/termos/<int:tid>/<action>")
    def term_action(tid, action):
        t = db.row("SELECT * FROM terms WHERE id = ?", tid) or abort(404)
        if action == "alternar":
            db.execute("UPDATE terms SET active = 1 - active WHERE id = ?", tid)
        elif action == "excluir":
            db.execute("DELETE FROM terms WHERE id = ?", tid)
        else:
            abort(404)
        return redirect(url_for("provider", pkey=t["provider"]))

    @app.route("/config/agendamentos")
    def jobs():
        return render_template("jobs.html", jobs=db.rows("SELECT * FROM jobs ORDER BY id"))

    def get_job(job_id):
        if job_id not in default_jobs():
            abort(404)

    def save_cron(job_id):
        """Salva cron/ativo do formulário. Retorna False se o cron for inválido."""
        cron = request.form["cron"].strip()
        try:
            scheduler.trigger(cron)
        except ValueError:
            flash(f"Cron inválido: {cron}")
            return False
        db.execute("UPDATE jobs SET cron = ?, enabled = ? WHERE id = ?", cron, int("enabled" in request.form), job_id)
        if scheduler.scheduler.running:
            scheduler.sync(job_id)
        return True

    @app.post("/config/agendamentos/<job_id>")
    def save_job(job_id):
        get_job(job_id)
        if save_cron(job_id):
            flash("Agendamento salvo.")
        return redirect(url_for("jobs"))

    @app.post("/config/agendamentos/<job_id>/executar")
    def run_job(job_id):
        get_job(job_id)
        scheduler.run_now(job_id)
        flash("Fluxo iniciado em segundo plano. Atualize a página para ver o resultado.")
        return redirect(url_for("jobs"))

    @app.route("/config/whatsapp", methods=["GET", "POST"])
    def whatsapp_config():
        if request.method == "POST":
            db.set_setting("whatsapp.url", request.form["url"].strip())
            if request.form.get("admin_token", "").strip():  # em branco mantém o token salvo
                db.set_setting("whatsapp.admin_token", request.form["admin_token"].strip())
            flash("Configuração do wuzapi salva.")
            return redirect(url_for("whatsapp_config"))
        bots = db.rows("SELECT b.*, (SELECT COUNT(*) FROM bot_groups g WHERE g.bot_id = b.id) AS groups "
                       "FROM bots b ORDER BY b.name")
        return render_template("whatsapp.html", url=db.get_setting("whatsapp.url"), bots=bots,
                               has_admin=bool(db.get_setting("whatsapp.admin_token")),
                               post_count=db.get_setting("whatsapp.post_count", "1"),
                               job=db.row("SELECT * FROM jobs WHERE id = ?", POST_JOB))

    @app.post("/config/whatsapp/postagem")
    def save_posting():
        count = request.form.get("post_count", type=int)
        if not count or count < 1:
            flash("Quantidade de produtos deve ser 1 ou mais.")
            return redirect(url_for("whatsapp_config"))
        if save_cron(POST_JOB):
            db.set_setting("whatsapp.post_count", str(count))
            chosen = set(request.form.getlist("bot", type=int))
            for b in db.rows("SELECT id FROM bots"):
                db.execute("UPDATE bots SET posts = ? WHERE id = ?", int(b["id"] in chosen), b["id"])
            flash("Fluxo de postagem salvo.")
        return redirect(url_for("whatsapp_config"))

    @app.post("/config/whatsapp/bots")
    def add_bot():
        name, token = request.form["name"].strip(), request.form["token"].strip()
        admin_token = db.get_setting("whatsapp.admin_token")
        if not name or not (token or admin_token):
            flash("Informe o nome e o token do bot, ou salve o token de admin do wuzapi para criá-lo automaticamente.")
            return redirect(url_for("whatsapp_config"))
        if not token:
            token = secrets.token_hex(16)
            try:
                whatsapp.create_user(db.get_setting("whatsapp.url"), admin_token, name, token)
            except Exception as e:
                flash(f"Não foi possível criar o bot no wuzapi: {e}")
                return redirect(url_for("whatsapp_config"))
        with db.connect() as con:
            bid = con.execute("INSERT INTO bots (name, token) VALUES (?, ?)", (name, token)).lastrowid
        flash("Bot cadastrado. Conecte o WhatsApp lendo o QR code.")
        return redirect(url_for("bot", bid=bid))

    def get_bot(bid):
        return db.row("SELECT * FROM bots WHERE id = ?", bid) or abort(404)

    @app.post("/config/whatsapp/bots/<int:bid>/excluir")
    def delete_bot(bid):
        get_bot(bid)
        db.execute("DELETE FROM bot_groups WHERE bot_id = ?", bid)
        db.execute("DELETE FROM bots WHERE id = ?", bid)
        flash("Bot excluído.")
        return redirect(url_for("whatsapp_config"))

    @app.route("/config/whatsapp/bots/<int:bid>", methods=["GET", "POST"])
    def bot(bid):
        b = get_bot(bid)
        if request.method == "POST":
            token = request.form.get("token", "").strip()
            db.execute("UPDATE bots SET name = ?, token = ? WHERE id = ?",
                       request.form["name"].strip() or b["name"], token or b["token"], bid)
            if "groups_form" in request.form:
                db.execute("DELETE FROM bot_groups WHERE bot_id = ?", bid)
                for value in request.form.getlist("group"):
                    jid, _, name = value.partition("|")
                    db.execute("INSERT INTO bot_groups (bot_id, jid, name) VALUES (?, ?, ?)", bid, jid, name)
            flash("Bot salvo.")
            return redirect(url_for("bot", bid=bid))
        selected = {g["jid"]: g["name"] for g in db.rows("SELECT * FROM bot_groups WHERE bot_id = ?", bid)}
        url, conn, qr = db.get_setting("whatsapp.url"), None, None
        try:
            conn = whatsapp.status(url, b["token"])
            if request.args.get("qr") and conn.get("connected") and not conn.get("loggedIn"):
                qr = whatsapp.qr(url, b["token"])
        except Exception as e:
            flash(f"Não foi possível consultar o wuzapi: {e}")
        groups = None
        if request.args.get("listar"):
            try:
                groups = whatsapp.list_groups(db.get_setting("whatsapp.url"), b["token"])
            except Exception as e:
                flash(f"Não foi possível listar os grupos: {e}")
        return render_template("bot.html", bot=b, selected=selected, groups=groups, conn=conn, qr=qr,
                               waiting_qr=bool(request.args.get("qr")) and not (conn or {}).get("loggedIn"))

    @app.post("/config/whatsapp/bots/<int:bid>/<action>")
    def bot_session(bid, action):
        b, url = get_bot(bid), db.get_setting("whatsapp.url")
        try:
            if action == "conectar":
                if not whatsapp.status(url, b["token"]).get("connected"):
                    whatsapp.connect(url, b["token"])
                return redirect(url_for("bot", bid=bid, qr=1))
            if action == "desconectar":
                whatsapp.logout(url, b["token"])
                flash("WhatsApp desconectado do bot.")
            else:
                abort(404)
        except RuntimeError as e:
            flash(f"Erro no wuzapi: {e}")
        return redirect(url_for("bot", bid=bid))

    return app
