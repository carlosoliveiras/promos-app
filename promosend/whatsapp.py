"""Cliente do wuzapi (https://github.com/asternic/wuzapi). Cada bot é um usuário do wuzapi com seu token."""
import base64

import requests


def _call(method, base_url, path, headers, **kw):
    if not base_url:
        raise RuntimeError("configure a URL do wuzapi")
    r = requests.request(method, base_url.rstrip("/") + path, headers=headers, timeout=60, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not r.ok or body.get("success") is False:
        raise RuntimeError(body.get("error") or f"HTTP {r.status_code}")
    return body.get("data") or {}


def image_data_url(url):
    """O wuzapi só aceita imagem embutida em base64."""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
    if mime not in ("image/jpeg", "image/png"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(r.content).decode()}"


def send_image(base_url, token, jid, image, caption):
    _call("POST", base_url, "/chat/send/image", {"Token": token}, json={"Phone": jid, "Image": image, "Caption": caption})


def list_groups(base_url, token):
    groups = _call("GET", base_url, "/group/list", {"Token": token}).get("Groups") or []
    return sorted(({"jid": g["JID"], "name": g.get("Name") or g["JID"]} for g in groups), key=lambda g: g["name"].lower())


def add_participant(base_url, token, group_jid, phone):
    # o wuzapi não informa o resultado por número: sucesso só quer dizer que o WhatsApp aceitou o pedido
    _call("POST", base_url, "/group/updateparticipants", {"Token": token},
          json={"GroupJID": group_jid, "Phone": [phone], "Action": "add"})


def create_user(base_url, admin_token, name, token):
    _call("POST", base_url, "/admin/users", {"Authorization": admin_token}, json={"name": name, "token": token})


def status(base_url, token):
    """{'connected': bool, 'loggedIn': bool}"""
    return _call("GET", base_url, "/session/status", {"Token": token})


def connect(base_url, token):
    _call("POST", base_url, "/session/connect", {"Token": token}, json={"Subscribe": ["Message"], "Immediate": True})


def qr(base_url, token):
    code = _call("GET", base_url, "/session/qr", {"Token": token}).get("QRCode") or ""
    return code if code.startswith("data:image/png;base64,") else None


def logout(base_url, token):
    _call("POST", base_url, "/session/logout", {"Token": token})
