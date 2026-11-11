"""Tabela de números no Supabase, via API REST (PostgREST). Colunas: numero e status."""
import requests


def _call(method, url, key, table, params, **kw):
    if not (url and key and table):
        raise RuntimeError("configure a URL, a chave e a tabela do Supabase")
    r = requests.request(method, f"{url.rstrip('/')}/rest/v1/{table}", params=params, timeout=30,
                         # só apikey: a chave nova (sb_secret_) não é JWT e o gateway monta o Authorization
                         headers={"apikey": key}, **kw)
    if not r.ok:
        raise RuntimeError(f"Supabase HTTP {r.status_code}: {r.text[:300]}")
    return r


def pending(url, key, table, limit):
    """Números ainda não processados: status vazio ou PENDENTE."""
    return [r["numero"] for r in _call("GET", url, key, table, {
        "select": "numero", "or": "(status.is.null,status.eq.PENDENTE)", "limit": limit}).json()]


def set_status(url, key, table, numero, status):
    _call("PATCH", url, key, table, {"numero": f"eq.{numero}"}, json={"status": status})
