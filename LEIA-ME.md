# Vigia de perfumes

Robô que verifica a Venba e a Olfactory Factory a cada 30 minutos e avisa no Telegram quando:

- um perfume vigiado **volta ao estoque**;
- o preço **cai 5% ou mais**, ou chega ao **menor preço já visto**;
- a loja coloca um **desconto novo** (preço "de/por");
- uma **marca seguida lança** um perfume novo.

Ele começa vigiando os 908 perfumes da planilha nessas duas lojas, e segue as marcas desses perfumes.

## Como colocar para funcionar

### 1. Criar o bot no Telegram
1. No Telegram, abra o **@BotFather** e envie `/newbot`.
2. Escolha um nome (ex.: Vigia Velor) e um usuário terminado em `bot` (ex.: vigia_velor_bot).
3. O BotFather responde com um **token**. Guarde, e não envie esse token para ninguém, nem no chat.

### 2. Criar o repositório no GitHub
1. Crie uma conta gratuita em github.com, se ainda não tiver.
2. Clique em **New repository**, dê o nome `vigia-perfumes`, marque **Private** e clique em **Create repository**.
3. Na página do repositório, clique em **uploading an existing file**.
4. Descompacte o zip e arraste **todo o conteúdo** da pasta `vigia-perfumes` (inclusive a pasta `.github`) para a página. Clique em **Commit changes**.

### 3. Guardar o token no GitHub
1. No repositório, vá em **Settings → Secrets and variables → Actions → New repository secret**.
2. Nome: `TELEGRAM_TOKEN`. Valor: o token do BotFather. Salve.
3. Ainda em **Settings → Actions → General**, em *Workflow permissions*, marque **Read and write permissions** e salve.

### 4. Ligar
1. No Telegram, abra o seu bot e envie `/start`.
2. No GitHub, abra a aba **Actions**, escolha **Vigia de perfumes** e clique em **Run workflow**.
3. Em cerca de 1 minuto o bot responde "Pronto!". A primeira rodada só registra os preços atuais; os avisos começam nas próximas.

## Comandos no Telegram
- `/status`: quantos perfumes estão sendo vigiados e a hora da última verificação
- `/marcas`: marcas seguidas para lançamentos
- `/seguir nome` e `/parar nome`: seguir ou deixar de seguir uma marca
- `/pausar` e `/continuar`: silenciar ou voltar os avisos

Os comandos são lidos na verificação seguinte, então a resposta pode levar até 30 minutos.

## Ajustes
Em `config.json`:
- `queda_minima`: queda de preço mínima para avisar (0.05 = 5%).
- `cotacao_padrao`: dólar usado se a cotação do dia não carregar.

## Limites
- A Niche Gallerie não entra, porque não tem a lista pública de produtos das outras duas lojas.
- Descontos que só aparecem no carrinho, como o automático da Mind Games, e cupons não são detectados.
- O GitHub pode atrasar as rodadas alguns minutos em horários de pico.
- Em repositório privado, o GitHub dá 2.000 minutos grátis por mês, e o robô usa cerca de 1.500. Se faltar, mude `*/30` para `*/45` em `.github/workflows/vigia.yml`.
- Se uma loja bloquear o acesso, o bot avisa depois de 3 tentativas seguidas que falharam.
