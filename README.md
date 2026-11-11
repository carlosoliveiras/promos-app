# PromoSend

Busca produtos de afiliados (Mercado Livre, Shopee e Amazon), gera links de afiliado e guarda tudo num SQLite local.
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
   - **Amazon**: sua tag de associado e, opcionalmente, os cookies da conta de associado. Em *URLs de mais
     vendidos*, cole as listas (`/gp/bestsellers/...`), uma por linha. Com cookies, o link sai curto (amzn.to)
     pela SiteStripe; sem cookies, ou se a SiteStripe falhar, sai o link longo com `?tag=`.
2. **Agendamentos**: cada fluxo de cada provedor tem seu próprio cron e começa **desativado**.
   - `busca`: pesquisa os termos ativos e grava os produtos.
   - `links` (Mercado Livre e Amazon): gera os links de afiliado dos produtos pendentes, de 20 em 20.
   - `limpeza`: apaga os produtos do provedor.
   Use *Executar agora* para testar um fluxo.
3. **WhatsApp** (via [wuzapi](https://github.com/asternic/wuzapi)): informe a URL do wuzapi e cadastre os bots
   (cada bot é um usuário do wuzapi, com seu token). Em *Configurar*, use *Listar grupos do bot* para ver os
   grupos do bot e marque em quais ele posta.
   - **Fluxo de postagem**: escolha os bots que postam, quantos produtos postar por execução e o cron.
     A cada execução o sistema sorteia produtos `PRONTO`, posta imagem + texto em todos os grupos dos bots
     marcados (e nos chats do Telegram), com intervalo aleatório de 1 a 5 s entre postagens, e marca o produto
     como `ENVIADO`.
4. **Telegram**: crie um bot no [@BotFather](https://t.me/BotFather), adicione-o como administrador nos grupos e
   canais e salve o token. Em *Chats*, coloque um por linha: `@canal` público ou o ID numérico. Para achar o ID,
   mande uma mensagem no grupo e use *Ver chats recentes do bot*. A postagem usa o mesmo fluxo do WhatsApp.
5. **Popular Grupo**: adiciona pessoas num grupo do WhatsApp a partir de uma tabela do Supabase com as colunas
   `numero` e `status`. Informe a URL do projeto, a chave secreta (`sb_secret_...` ou a antiga `service_role`) e a tabela (padrão `numeros`), marque os
   bots (todos administradores do grupo), salve e use *Listar grupos dos bots* para escolher o grupo.
   A cada execução o sistema pega os números com status vazio ou `PENDENTE`, adiciona um por um revezando entre
   os bots, com intervalo aleatório de 1 a 4 s, e grava `ADICIONADO` ou `ERRO` na tabela.
6. **Produtos**: a tela inicial mostra os produtos em cards, com filtro e paginação.

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
