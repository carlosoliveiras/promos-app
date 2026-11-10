"""Cliente da Bot API do Telegram. Um bot (token do @BotFather) posta em vários grupos e canais."""
import requests


def _call(token, method, **data):
    if not token:
        raise RuntimeError("configure o token do bot do Telegram")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=60)
    except requests.RequestException as e:  # a mensagem traria a URL, que contém o token
        raise RuntimeError(f"falha de conexão com o Telegram ({type(e).__name__})") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or f"HTTP {r.status_code}")
    return body["result"]


def send_photo(token, chat_id, photo_url, caption):
    """O próprio Telegram baixa a imagem da URL."""
    _call(token, "sendPhoto", chat_id=chat_id, photo=photo_url, caption=caption, parse_mode="HTML")


def recent_chats(token):
    """Grupos e canais que mandaram algo ao bot nas últimas 24 h; a Bot API não lista os chats do bot."""
    chats = {}
    for u in _call(token, "getUpdates", allowed_updates=["message", "channel_post", "my_chat_member"]):
        for key in ("message", "channel_post", "my_chat_member"):
            c = (u.get(key) or {}).get("chat")
            if c and c.get("type") != "private":
                chats[str(c["id"])] = c.get("title") or c.get("username") or str(c["id"])
    return sorted(({"id": i, "name": n} for i, n in chats.items()), key=lambda c: c["name"].lower())
