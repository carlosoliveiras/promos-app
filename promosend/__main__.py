import logging
import os

from waitress import serve

from . import scheduler
from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# shortcut: agendador roda no mesmo processo; use só 1 processo (não rode com vários workers)
app = create_app()
scheduler.start()
host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
logging.info("PromoSend em http://%s:%s", host, port)
try:
    serve(app, host=host, port=port)
except OSError as e:
    raise SystemExit(f"Não foi possível usar {host}:{port} ({e.strerror}). Escolha outra porta: PORT=8010 python -m promosend")
