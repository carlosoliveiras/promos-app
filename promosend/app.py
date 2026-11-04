import secrets

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, scheduler, whatsapp
from .flows import FLOW_LABELS, POST_JOB, brl, default_jobs
from .providers import PROVIDERS

PUBLIC = {"login", "setup", "static"}
PAGE_SIZE = 24


def create_app():
    db.init()
    app = Flask(__name__)
    if not db.get_setting("secret_key"):
        db.set_setting("secret_key", secrets.token_hex(32))
    app.secret_key = db.get_setting("secret_key")
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, PROVIDERS=PROVIDERS, FLOW_LABELS=FLOW_LABELS)
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
        return render_template("provider.html", pkey=pkey, prov=prov, cfg=cfg, terms=terms)

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
            flash("URL do wuzapi salva.")
            return redirect(url_for("whatsapp_config"))
        bots = db.rows("SELECT b.*, (SELECT COUNT(*) FROM bot_groups g WHERE g.bot_id = b.id) AS groups "
                       "FROM bots b ORDER BY b.name")
        return render_template("whatsapp.html", url=db.get_setting("whatsapp.url"), bots=bots,
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
        if name and token:
            db.execute("INSERT INTO bots (name, token) VALUES (?, ?)", name, token)
            flash("Bot cadastrado.")
        return redirect(url_for("whatsapp_config"))

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
        groups = None
        if request.args.get("listar"):
            try:
                groups = whatsapp.list_groups(db.get_setting("whatsapp.url"), b["token"])
            except Exception as e:
                flash(f"Não foi possível listar os grupos: {e}")
        return render_template("bot.html", bot=b, selected=selected, groups=groups)

    return app
