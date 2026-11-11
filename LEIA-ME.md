# Vigia de perfumes

Robô que a cada 30 minutos lê as lojas e avisa no Telegram, com foto e botões, quando:

- um perfume vigiado **volta ao estoque**;
- o preço **cai 5% ou mais**, ou chega ao **menor preço já visto**;
- a loja coloca um **desconto novo** (preço "de/por");
- uma **marca seguida lança** um perfume novo;
- um **cupom passa a valer ou para de valer**, ou surge um **desconto automático** no carrinho (teste diário, a partir das 9h).

Ele também mantém o **catálogo** em `docs/` atualizado, publicado pelo GitHub Pages.

## No Telegram
- Mande o **link de um perfume** (pode encaminhar do WhatsApp, inclusive links encurtados) para começar a vigiar.
- Mande o link de uma **loja nova** que use Shopify ou WooCommerce e ela entra na lista.
- Botões em cada aviso: **Ver na loja**, **Parar de seguir a marca** e **Não vigiar este**.
- Comandos: `/status`, `/lojas`, `/marcas`, `/seguir nome`, `/parar nome`, `/cupons`, `/cupom CODIGO loja`, `/pausar`, `/continuar`.

As respostas chegam na verificação seguinte (até 30 minutos).

## Configuração
- Segredo do repositório: `TELEGRAM_TOKEN` (token do @BotFather).
- *Settings › Actions › General › Workflow permissions*: **Read and write**.
- *Settings › Pages*: *Deploy from a branch*, branch `main`, pasta `/docs`.

Em `config.json`: lojas, cupons testados todo dia, queda mínima para avisar (0.05 = 5%), hora do teste de cupons e quantos avisos com foto mandar por rodada.

## Limites
- Lojas WooCommerce (como a Niche Gallerie) são lidas produto a produto, então não avisam lançamentos. Os preços delas são convertidos para dólar pela cotação fixa da moeda local, e podem diferir um pouco do que o site mostra.
- Lojas que não usam Shopify nem WooCommerce (como a Beautyliv) não são lidas.
- Se uma loja bloquear o acesso, o bot avisa depois de 3 tentativas seguidas que falharem.
