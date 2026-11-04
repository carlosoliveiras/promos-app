"""Cliente do wuzapi (https://github.com/asternic/wuzapi). Cada bot é um usuário do wuzapi com seu token."""
import base64

import requests


def image_data_url(url):
    """O wuzapi só aceita imagem embutida em base64."""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
    if mime not in ("image/jpeg", "image/png"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(r.content).decode()}"


def send_image(base_url, token, jid, image, caption):
    r = requests.post(base_url.rstrip("/") + "/chat/send/image", headers={"Token": token}, timeout=60,
                      json={"Phone": jid, "Image": image, "Caption": caption})
    r.raise_for_status()


def list_groups(base_url, token):
    if not base_url:
        raise RuntimeError("configure a URL do wuzapi")
    r = requests.get(base_url.rstrip("/") + "/group/list", headers={"Token": token}, timeout=30)
    r.raise_for_status()
    groups = (r.json().get("data") or {}).get("Groups") or []
    return sorted(({"jid": g["JID"], "name": g.get("Name") or g["JID"]} for g in groups), key=lambda g: g["name"].lower())
