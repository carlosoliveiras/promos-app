"""Provedores de produtos.

Cada provedor é um módulo com:
    NAME, TERM_LABEL, TERM_HELP  textos da interface
    FIELDS          campos de configuração: {key, label, type, secret?, default?}
    DEFAULT_CRONS   cron padrão de cada fluxo que o provedor suporta (busca, links, limpeza)
    SEARCH_DELAY    segundos de espera entre um termo e outro na busca
    search(term, cfg) -> list[dict]        produtos já filtrados
    accepts(term, cfg) -> bool             opcional: se o termo vale para a configuração atual
    generate_links(urls, cfg) -> iter      opcional: (url, link_afiliado | None, erro | None) por URL

Para adicionar um provedor, crie o módulo e registre em PROVIDERS.
"""


def require(cfg, *keys):
    missing = [k for k in keys if not str(cfg.get(k, "")).strip()]
    if missing:
        raise RuntimeError("configuração faltando: " + ", ".join(missing))


from . import amazon, mercadolivre, shopee  # noqa: E402  (módulos importam require acima)

PROVIDERS = {"mercadolivre": mercadolivre, "shopee": shopee, "amazon": amazon}
