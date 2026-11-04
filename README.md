# PromoSend

Busca produtos de afiliados (Mercado Livre e Shopee), gera links de afiliado e guarda tudo num SQLite local.
Feito para cada pessoa hospedar na própria máquina.

## Instalação

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m promosend
```

Abra http://127.0.0.1:8000. No primeiro acesso o sistema pede para criar o usuário administrador.

Variáveis de ambiente opcionais:

| Variável        | Padrão         | Uso                                                       |
|-----------------|----------------|-----------------------------------------------------------|
| `HOST`          | `127.0.0.1`    | Use `0.0.0.0` para acessar de outra máquina da rede       |
| `PORT`          | `8000`         | Porta HTTP                                                |
| `PROMOSEND_DB`  | `promosend.db` | Caminho do banco SQLite                                   |

Rode só **um** processo: o agendador dos fluxos roda dentro dele.

## Uso

1. **Provedores**: clique no provedor e preencha as credenciais.
   - **Shopee**: App ID e Secret da API de afiliados, mais os filtros (preço, comissão e vendas mínimas).
     Em *Palavras-chave*, cole uma lista com um termo por linha. Cada termo vira uma busca.
   - **Mercado Livre**: cookies da conta de afiliado, `x-csrf-token` (copie do DevTools na página
     *linkbuilder*) e sua tag de afiliado. Em *URLs de categoria*, cole as URLs de listagem, uma por linha.
     Os termos podem ter um *grupo*, que fica salvo junto dos produtos.
2. **Agendamentos**: cada fluxo de cada provedor tem seu próprio cron e começa **desativado**.
   - `busca`: pesquisa os termos ativos e grava os produtos.
   - `links` (só Mercado Livre): gera os links de afiliado dos produtos pendentes, de 20 em 20.
   - `limpeza`: apaga os produtos do provedor.
   Use *Executar agora* para testar um fluxo.
3. **WhatsApp** (via [wuzapi](https://github.com/asternic/wuzapi)): informe a URL do wuzapi e cadastre os bots
   (cada bot é um usuário do wuzapi, com seu token). Em *Configurar*, use *Listar grupos do bot* para ver os
   grupos do bot e marque em quais ele posta.
   - **Fluxo de postagem**: escolha os bots que postam, quantos produtos postar por execução e o cron.
     A cada execução o sistema sorteia produtos `PRONTO`, posta imagem + texto em todos os grupos dos bots
     marcados, com intervalo aleatório de 1 a 5 s entre postagens, e marca o produto como `ENVIADO`.
4. **Produtos**: a tela inicial mostra os produtos em cards, com filtro e paginação.

Status dos produtos: `PENDENTE` (sem link de afiliado), `PRONTO` (com link) e `ENVIADO`.

## Segurança

- As credenciais ficam em texto puro no `promosend.db`. Proteja esse arquivo e não faça commit dele.
- Por padrão o servidor só escuta em `127.0.0.1`. Se expuser na internet, coloque atrás de um proxy com HTTPS.

## Adicionar um provedor

Crie um módulo em `promosend/providers/` seguindo a interface descrita em
`promosend/providers/__init__.py` e registre-o em `PROVIDERS`. As telas, os termos e os
agendamentos aparecem sozinhos.

## Testes

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests
```
