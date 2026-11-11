"""Vigia de perfumes (versão 2).

A cada rodada (30 em 30 minutos, pelo GitHub Actions):
- lê as lojas e compara com a rodada anterior;
- avisa no Telegram, com foto e botões, quando um perfume volta ao estoque,
  baixa de preço, ganha desconto ou quando uma marca seguida lança algo;
- lê mensagens do Telegram: comandos, links de perfumes ou de lojas novas
  e toques nos botões;
- uma vez por dia testa os cupons e os descontos automáticos no carrinho;
- atualiza o catálogo do site (docs/dados.json).

Segredo necessário: TELEGRAM_TOKEN. Teste sem enviar nada: DRY_RUN=1 python vigia.py
"""
import datetime
import hashlib
import html
import http.cookiejar as cookiejar
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_ESTADO = os.path.join(PASTA, "estado.json")
ARQ_CATALOGO = os.path.join(PASTA, "catalogo.json")
ARQ_CONFIG = os.path.join(PASTA, "config.json")
ARQ_DADOS_SITE = os.path.join(PASTA, "docs", "dados.json")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BRT = datetime.timezone(datetime.timedelta(hours=-3))

PEQUENO = re.compile(r"sample|decant|atomi[sz]er|travel|discovery|amostra", re.I)
ML = re.compile(r"(\d+(?:[.,]\d+)?)\s*ml", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"']+")
TAXAS_FIXAS = {"USD": 1.0, "AED": 3.6725, "SAR": 3.75, "QAR": 3.64}


# =============================================================== utilidades
def agora():
    return datetime.datetime.now(BRT)


def ler_json(caminho, padrao):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def salvar_json(caminho, dados, compacto=False):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compacto:
            json.dump(dados, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(dados, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, caminho)


def norm(texto):
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def dinheiro(v, prefixo):
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{prefixo} {s}"


def usd(v):
    return dinheiro(v, "US$")


def brl(v):
    return dinheiro(v, "R$")


# =============================================================== http
def http(url, metodo="GET", corpo=None, json_corpo=None, opener=None, tentativas=3):
    """Devolve (status, bytes, url_final). Erros 4xx voltam como status, sem exceção."""
    cab = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    if json_corpo is not None:
        corpo = json.dumps(json_corpo).encode()
        cab["Content-Type"] = "application/json"
    abrir = (opener or urllib.request.build_opener()).open
    ultimo = None
    for n in range(tentativas):
        try:
            req = urllib.request.Request(url, data=corpo, headers=cab, method=metodo)
            with abrir(req, timeout=40) as r:
                return r.status, r.read(), r.geturl()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                ultimo = e
                time.sleep(5 * (n + 1))
                continue
            return e.code, e.read() if hasattr(e, "read") else b"", url
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            ultimo = e
            time.sleep(3 * (n + 1))
    raise RuntimeError(f"sem resposta de {urllib.parse.urlsplit(url).netloc}: {type(ultimo).__name__}")


def http_json(url, **kw):
    status, dados, _ = http(url, **kw)
    if status != 200:
        raise RuntimeError(f"HTTP {status} em {urllib.parse.urlsplit(url).path}")
    return json.loads(dados.decode("utf-8"))


# =============================================================== lojas
def variante_pequena(titulo):
    if PEQUENO.search(titulo or ""):
        return True
    m = ML.search(titulo or "")
    return bool(m) and float(m.group(1).replace(",", ".")) <= 10


def limpar_titulo(titulo, marca):
    if marca and titulo.lower().startswith(marca.lower()):
        titulo = titulo[len(marca):].lstrip(" -–:") or titulo
    return titulo


def resumir_shopify(prod):
    """Reduz um produto Shopify ao frasco principal: o maior disponível, ou o maior de todos."""
    vs = [v for v in prod.get("variants", []) if not variante_pequena(v.get("title"))] or prod.get("variants", [])
    if not vs:
        return None
    vs = sorted(vs, key=lambda v: float(v.get("price") or 0), reverse=True)
    disp = [v for v in vs if v.get("available")]
    v = disp[0] if disp else vs[0]
    preco = float(v.get("price") or 0)
    cheio = float(v.get("compare_at_price") or 0)
    tam = ML.search(v.get("title") or "")
    imgs = prod.get("images") or []
    img = imgs[0].get("src", "") if imgs and isinstance(imgs[0], dict) else ""
    marca = prod.get("vendor", "")
    return {
        "t": limpar_titulo(prod.get("title", ""), marca),
        "m": marca,
        "p": preco,
        "c": cheio if cheio > preco else 0,
        "a": bool(disp),
        "s": (tam.group(1) + " ml") if tam else "",
        "tester": "tester" in (v.get("title", "") + " " + prod.get("title", "")).lower(),
        "img": img,
        "var": v.get("id"),
    }


def shopify_js_para_json(d):
    """Converte o /products/x.js (preços em centavos) para o formato do catálogo."""
    def img(s):
        s = s if isinstance(s, str) else (s or {}).get("src", "")
        return "https:" + s if s.startswith("//") else s
    return {
        "handle": d.get("handle"), "title": d.get("title", ""), "vendor": d.get("vendor", ""),
        "images": [{"src": img(s)} for s in d.get("images", [])],
        "variants": [{"id": v.get("id"), "title": v.get("title", ""), "price": f"{(v.get('price') or 0) / 100:.2f}",
                      "compare_at_price": f"{v['compare_at_price'] / 100:.2f}" if v.get("compare_at_price") else None,
                      "available": v.get("available")} for v in d.get("variants", [])],
    }


def ler_shopify(base, max_paginas=80):
    produtos = []
    for pagina in range(1, max_paginas + 1):
        lote = http_json(f"{base}/products.json?limit=250&page={pagina}").get("products", [])
        if not lote:
            break
        produtos.extend(lote)
        time.sleep(1)
    if not produtos:
        raise RuntimeError("catálogo vazio")
    return {p["handle"]: p for p in produtos}


def taxa_para_usd(moeda, cache={}):
    moeda = (moeda or "USD").upper()
    if moeda in TAXAS_FIXAS:
        return TAXAS_FIXAS[moeda]
    if "tabela" not in cache:
        try:
            cache["tabela"] = http_json("https://open.er-api.com/v6/latest/USD", tentativas=2).get("rates", {})
        except Exception:  # noqa: BLE001
            cache["tabela"] = {}
    return cache["tabela"].get(moeda)


def resumir_woo(prod, marcas_conhecidas):
    pr = prod.get("prices") or {}
    div = 10 ** int(pr.get("currency_minor_unit") or 0)
    taxa = taxa_para_usd(pr.get("currency_code"))
    if not taxa:
        return None
    preco = float(pr.get("price") or 0) / div / taxa
    cheio = float(pr.get("regular_price") or 0) / div / taxa
    nome = html.unescape(prod.get("name", ""))
    marca = ""
    for m in sorted(marcas_conhecidas, key=len, reverse=True):
        if m and norm(nome).startswith(norm(m) + " "):
            marca = m
            break
    if not marca:
        marcas = prod.get("brands") or []
        marca = marcas[0].get("name", "") if marcas else nome.split(" ")[0]
    tam = ML.search(nome)
    imgs = prod.get("images") or []
    return {
        "t": limpar_titulo(nome, marca),
        "m": marca,
        "p": round(preco, 2),
        "c": round(cheio, 2) if cheio > preco + 0.01 else 0,
        "a": bool(prod.get("is_in_stock")),
        "s": (tam.group(1) + " ml") if tam else "",
        "tester": "tester" in nome.lower(),
        "img": imgs[0].get("src", "") if imgs else "",
        "var": None,
    }


def ler_woo_produto(base, slug):
    lista = http_json(f"{base}/wp-json/wc/store/v1/products?slug={urllib.parse.quote(slug)}")
    return lista[0] if lista else None


# =============================================================== telegram
ENVIADAS = []


def tg(metodo, **params):
    if DRY_RUN or not TOKEN:
        return {"ok": True, "result": []}
    url = f"https://api.telegram.org/bot{TOKEN}/{metodo}"
    try:
        status, dados, _ = http(url, metodo="POST", corpo=urllib.parse.urlencode(params).encode())
        return json.loads(dados.decode())
    except Exception as e:  # noqa: BLE001
        print("Telegram falhou:", metodo, type(e).__name__)
        return {"ok": False, "result": []}


def enviar(chat_id, texto, botoes=None, foto=None):
    ENVIADAS.append({"texto": texto, "botoes": botoes, "foto": foto})
    if DRY_RUN:
        print("---- mensagem" + (" com foto" if foto else "") + " ----\n" + re.sub(r"<[^>]+>", "", texto))
        if botoes:
            print("   [" + "] [".join(b["text"] for linha in botoes for b in linha) + "]")
        return
    if not chat_id:
        return
    extra = {"reply_markup": json.dumps({"inline_keyboard": botoes})} if botoes else {}
    r = {"ok": False}
    if foto:
        r = tg("sendPhoto", chat_id=chat_id, photo=foto, caption=texto[:1024], parse_mode="HTML", **extra)
    if not r.get("ok"):
        tg("sendMessage", chat_id=chat_id, text=texto, parse_mode="HTML", disable_web_page_preview="true", **extra)
    time.sleep(1)


def enviar_em_partes(chat_id, titulo, linhas, limite=3800):
    bloco = titulo
    for linha in linhas:
        if len(bloco) + len(linha) + 2 > limite:
            enviar(chat_id, bloco)
            bloco = titulo + " (continuação)"
        bloco += "\n\n" + linha
    if bloco.strip() != titulo:
        enviar(chat_id, bloco)


AJUDA = (
    "<b>Comandos</b>\n"
    "/status: o que estou vigiando\n"
    "/lojas: lojas que eu leio\n"
    "/marcas: marcas seguidas para lançamentos\n"
    "/seguir nome e /parar nome: seguir ou deixar de seguir uma marca\n"
    "/cupons: cupons e descontos automáticos de hoje\n"
    "/cupom CODIGO loja: incluir um cupom para eu testar todo dia\n"
    "/pausar e /continuar: silenciar ou voltar os avisos\n\n"
    "Para vigiar um perfume ou uma loja nova, é só me mandar o link (pode encaminhar do WhatsApp).\n"
    "Eu verifico a cada 30 minutos e respondo na verificação seguinte."
)


# =============================================================== estado e lojas
class Vigia:
    def __init__(self, http_fn=None):
        global http
        if http_fn:
            http = http_fn  # para os testes
        self.cfg = ler_json(ARQ_CONFIG, {})
        self.queda = float(self.cfg.get("queda_minima", 0.05))
        self.est = ler_json(ARQ_ESTADO, {})
        e = self.est
        for k, v in (("produtos", {}), ("conhecidos", {}), ("falhas", {}), ("lojas_extra", {}), ("cupons", {}),
                     ("auto", {}), ("cupons_extra", []), ("cb", {}), ("cb_n", 0)):
            e.setdefault(k, v)
        cat = ler_json(ARQ_CATALOGO, {"produtos": []})["produtos"]
        self.marca_cat = {f"{p['loja']}:{p['handle']}": p.get("marca", "") for p in cat}
        if "vigiados" not in e:
            e["vigiados"] = sorted(self.marca_cat)
        if e.get("versao") != 2:  # estado da versão 1: inclui os perfumes novos do catálogo
            e["vigiados"] = sorted(set(e["vigiados"]) | set(self.marca_cat))
            e["versao"] = 2
        self.vig = set(e["vigiados"])
        self.chat = e.get("chat_id")
        self.avisos = []
        self.leituras = {}
        self.cotacao = None

    # ---------- lojas
    def lojas(self):
        todas = dict(self.cfg.get("lojas", {}))
        todas.update(self.est["lojas_extra"])
        return todas

    def loja_do_host(self, host):
        host = host.lower().removeprefix("www.")
        for k, l in self.lojas().items():
            if urllib.parse.urlsplit(l["url"]).netloc.lower().removeprefix("www.") == host:
                return k
        return None

    def url_produto(self, chave_loja, ident):
        l = self.lojas()[chave_loja]
        if l["tipo"] == "shopify":
            return f"{l['url']}/products/{ident}"
        return self.est["produtos"].get(f"{chave_loja}:{ident}", {}).get("url") or f"{l['url']}/?p={ident}"

    def todas_marcas(self):
        ms = {p.get("vendor", "") for l in self.leituras.values() for p in l.values() if p.get("vendor")}
        ms |= {v for v in self.marca_cat.values() if v}
        return sorted(ms, key=norm)

    def cb(self, acao, valor):
        self.est["cb_n"] = (self.est["cb_n"] + 1) % 100000
        cid = str(self.est["cb_n"])
        self.est["cb"][cid] = [acao, valor]
        if len(self.est["cb"]) > 600:
            for velho in sorted(self.est["cb"], key=int)[:200]:
                self.est["cb"].pop(velho, None)
        return cid

    # ---------- leitura
    def ler_lojas(self):
        for chave, loja in self.lojas().items():
            try:
                if loja["tipo"] == "shopify":
                    prods = ler_shopify(loja["url"])
                    antes = len(self.est["conhecidos"].get(chave, []))
                    if antes and len(prods) < antes * 0.7:
                        raise RuntimeError(f"catálogo veio incompleto ({len(prods)} de {antes})")
                    self.leituras[chave] = prods
                else:
                    self.leituras[chave] = self.ler_woo(chave, loja)
                self.est["falhas"][chave] = 0
            except Exception as e:  # noqa: BLE001
                print(f"Falha ao ler {loja['nome']}: {e}")
                self.est["falhas"][chave] = self.est["falhas"].get(chave, 0) + 1

    def ler_woo(self, chave, loja):
        """WooCommerce não mostra o catálogo inteiro; lê um a um os produtos vigiados."""
        out, erros = {}, 0
        for k in sorted(self.vig):
            if not k.startswith(chave + ":"):
                continue
            slug = k.split(":", 1)[1]
            try:
                p = ler_woo_produto(loja["url"], slug)
                if p:
                    p["_woo"] = True
                    out[slug] = p
            except Exception:  # noqa: BLE001
                erros += 1
            time.sleep(1)
        if erros and not out:
            raise RuntimeError("nenhum produto respondeu")
        return out

    def resumir(self, chave, prod):
        if prod.get("_woo"):
            item = resumir_woo(prod, self.todas_marcas())
            if item:
                item["url"] = prod.get("permalink", "")
            return item
        return resumir_shopify(prod)

    # ---------- comparação com a rodada anterior
    def comparar(self):
        seguidas = {norm(m) for m in self.est.get("marcas", [])}
        hoje = agora().strftime("%Y-%m-%d")
        for chave, prods in self.leituras.items():
            loja = self.lojas()[chave]
            shopify = loja["tipo"] == "shopify"
            conhecidos = set(self.est["conhecidos"].get(chave, []))
            ja_lido = chave in self.est["conhecidos"]
            avisar_novos = len(set(prods) - conhecidos) <= 40
            for h, prod in prods.items():
                k = f"{chave}:{h}"
                item = self.resumir(chave, prod)
                if item is None:
                    continue
                if shopify and ja_lido and h not in conhecidos and norm(item["m"]) in seguidas and k not in self.vig:
                    self.vig.add(k)
                    if avisar_novos:
                        st = "em estoque" if item["a"] else "ainda esgotado"
                        self.aviso("lancamento", k, item, loja, f"Novo na loja, {st}: {usd(item['p'])}")
                if k not in self.vig:
                    continue
                ant = self.est["produtos"].get(k)
                if ant is not None and "p" in ant:
                    if not ant["a"] and item["a"]:
                        self.aviso("estoque", k, item, loja, f"Voltou ao estoque por <b>{usd(item['p'])}</b>")
                    elif item["a"] and item["p"] > 0:
                        menor = ant.get("min", ant["p"])
                        caiu = item["p"] <= ant["p"] * (1 - self.queda)
                        recorde = item["p"] < menor - 0.009
                        if caiu or recorde:
                            txt = f"{usd(ant['p'])} → <b>{usd(item['p'])}</b>"
                            if item["p"] < ant["p"]:
                                txt += f" (-{round((1 - item['p'] / ant['p']) * 100)}%)"
                            if recorde:
                                txt += ", menor preço já visto"
                            self.aviso("preco", k, item, loja, txt)
                        elif item["c"] and (not ant.get("c") or item["c"] - item["p"] > ant["c"] - ant["p"] + 0.5):
                            off = round((1 - item["p"] / item["c"]) * 100)
                            self.aviso("desconto", k, item, loja, f"De {usd(item['c'])} por <b>{usd(item['p'])}</b> (-{off}%)")
                    item["min"] = min(ant.get("min", ant["p"]), item["p"]) if item["p"] > 0 else ant.get("min", 0)
                    item["visto"] = ant.get("visto", hoje)
                else:
                    item["min"] = item["p"]
                    item["visto"] = hoje
                self.est["produtos"][k] = item
            if shopify:
                self.est["conhecidos"][chave] = sorted(set(prods))

    def aviso(self, tipo, k, item, loja, texto):
        self.avisos.append({"tipo": tipo, "k": k, "item": item, "loja": loja, "texto": texto})

    # ---------- envio dos avisos
    def legenda(self, a):
        it = a["item"]
        marca = self.marca_cat.get(a["k"]) or it["m"]
        det = " · ".join(x for x in (it.get("s"), "tester" if it.get("tester") else "") if x)
        nome = f"<b>{html.escape(marca)}</b> {html.escape(it['t'])}" + (f" ({det})" if det else "")
        return f"{nome}\n{a['texto']} · {brl(it['p'] * self.cot())}\n{html.escape(a['loja']['nome'])}"

    def botoes(self, a):
        it = a["item"]
        marca = self.marca_cat.get(a["k"]) or it["m"]
        url = it.get("url") or self.url_produto(a["k"].split(":")[0], a["k"].split(":", 1)[1])
        linha2 = [{"text": "Não vigiar este", "callback_data": self.cb("nao_vigiar", a["k"])}]
        if marca:
            linha2.insert(0, {"text": f"Parar de seguir {marca}"[:40], "callback_data": self.cb("parar", marca)})
        return [[{"text": f"Ver na {a['loja']['nome']}", "url": url}], linha2]

    def enviar_avisos(self):
        if not self.chat or self.est.get("pausado") or not self.avisos:
            return
        titulos = {"estoque": "🟢 Voltou ao estoque", "preco": "🔻 Baixou de preço", "desconto": "🏷️ Desconto novo",
                   "lancamento": "✨ Lançamento", "cupom": "🎟️ Cupom", "auto": "🎁 Desconto automático"}
        ordem = ["estoque", "preco", "cupom", "auto", "desconto", "lancamento"]
        lista = sorted(self.avisos, key=lambda a: ordem.index(a["tipo"]))
        limite = int(self.cfg.get("max_fotos", 8))
        for a in lista[:limite]:
            if a.get("item"):
                img = a["item"].get("img", "")
                if img and "cdn.shopify.com" in img:
                    img += ("&" if "?" in img else "?") + "width=800"
                enviar(self.chat, f"{titulos[a['tipo']]}\n\n" + self.legenda(a), botoes=self.botoes(a), foto=img or None)
            else:
                enviar(self.chat, f"{titulos[a['tipo']]}\n\n{a['texto']}")
        resto = lista[limite:]
        if resto:
            linhas = []
            for a in resto:
                if a.get("item"):
                    url = a["item"].get("url") or self.url_produto(a["k"].split(":")[0], a["k"].split(":", 1)[1])
                    linhas.append(f"{titulos[a['tipo']]}: " + self.legenda(a) + f"\n<a href=\"{url}\">Abrir</a>")
                else:
                    linhas.append(f"{titulos[a['tipo']]}: {a['texto']}")
            enviar_em_partes(self.chat, f"<b>Mais {len(resto)} avisos</b>", linhas)

    def cot(self):
        if self.cotacao is None:
            try:
                d = http_json("https://economia.awesomeapi.com.br/json/last/USD-BRL", tentativas=2)
                self.cotacao = round(float(d["USDBRL"]["bid"]), 4)
            except Exception:  # noqa: BLE001
                self.cotacao = float(self.cfg.get("cotacao_padrao", 5.5))
        return self.cotacao

    # ---------- telegram: comandos, links e botões
    def tratar_telegram(self):
        r = tg("getUpdates", offset=self.est.get("offset", 0), timeout=0,
               allowed_updates=json.dumps(["message", "callback_query"]))
        for upd in r.get("result", []):
            self.est["offset"] = upd["update_id"] + 1
            if "callback_query" in upd:
                self.tratar_botao(upd["callback_query"])
                continue
            msg = upd.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            texto = (msg.get("text") or msg.get("caption") or "").strip()
            if not chat:
                continue
            if self.chat is None and texto.startswith("/start"):
                self.chat = self.est["chat_id"] = chat
                enviar(chat, "Pronto! Vou te avisar aqui quando um perfume voltar ao estoque, baixar de preço ou ganhar desconto.\n\n" + AJUDA)
                continue
            if chat != self.chat:
                continue  # só obedece ao dono
            urls = URL_RE.findall(texto)
            for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
                if ent.get("type") == "text_link" and ent.get("url"):
                    urls.append(ent["url"])
            if texto.startswith("/"):
                self.comando(texto)
            elif urls:
                for u in dict.fromkeys(x.rstrip(".,;)*_") for x in urls):
                    self.adicionar_link(u)

    def tratar_botao(self, q):
        tg("answerCallbackQuery", callback_query_id=q.get("id", ""))
        if (q.get("from") or {}).get("id") != self.chat and ((q.get("message") or {}).get("chat") or {}).get("id") != self.chat:
            return
        acao = self.est["cb"].get(str(q.get("data", "")))
        if not acao:
            enviar(self.chat, "Esse botão é antigo. Use /parar nome-da-marca se precisar.")
            return
        tipo, valor = acao
        if tipo == "parar":
            antes = len(self.est.get("marcas", []))
            self.est["marcas"] = [m for m in self.est.get("marcas", []) if norm(m) != norm(valor)]
            alvo = {k for k, p in self.est["produtos"].items() if norm(self.marca_cat.get(k) or p.get("m", "")) == norm(valor)}
            self.vig -= alvo
            enviar(self.chat, f"Pronto, parei de seguir {html.escape(valor)} ({len(alvo)} perfumes)."
                   + ("" if len(self.est["marcas"]) < antes or alvo else ""))
        elif tipo == "nao_vigiar":
            self.vig.discard(valor)
            nome = self.est["produtos"].get(valor, {}).get("t", valor)
            enviar(self.chat, f"Ok, não vigio mais {html.escape(nome)}.")

    def comando(self, texto):
        cmd, _, arg = texto.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        seguidas = set(self.est.setdefault("marcas", []))
        if cmd in ("/ajuda", "/help", "/start"):
            enviar(self.chat, AJUDA)
        elif cmd == "/status":
            vig = [self.est["produtos"][k] for k in self.vig if k in self.est["produtos"]]
            n_ok = sum(1 for p in vig if p.get("a"))
            enviar(self.chat, f"Vigiando <b>{len(self.vig)}</b> perfumes ({n_ok} em estoque) em {len(self.lojas())} lojas, "
                              f"e {len(seguidas)} marcas para lançamentos.\nÚltima verificação: {self.est.get('ultima', '-')} (Brasília)."
                   + ("\n\nAvisos pausados. Use /continuar." if self.est.get("pausado") else ""))
        elif cmd == "/lojas":
            linhas = [f"{html.escape(l['nome'])}: {sum(1 for k in self.vig if k.startswith(c + ':'))} perfumes"
                      for c, l in self.lojas().items()]
            enviar(self.chat, "<b>Lojas</b>\n" + "\n".join(linhas))
        elif cmd == "/marcas":
            enviar_em_partes(self.chat, "<b>Marcas seguidas</b>", [html.escape(", ".join(sorted(seguidas, key=norm)))])
        elif cmd in ("/seguir", "/parar"):
            if not arg:
                enviar(self.chat, f"Diga a marca, por exemplo: {cmd} xerjoff")
                return
            achadas = sorted({v for v in self.todas_marcas() if norm(arg) in norm(v)}) or [arg]
            if cmd == "/seguir":
                seguidas.update(achadas)
                enviar(self.chat, "Seguindo: " + html.escape(", ".join(achadas)))
            else:
                alvo = {s for s in seguidas if norm(arg) in norm(s)}
                seguidas -= alvo
                enviar(self.chat, ("Deixei de seguir: " + html.escape(", ".join(sorted(alvo)))) if alvo else "Não achei essa marca na lista.")
            self.est["marcas"] = sorted(seguidas, key=norm)
        elif cmd == "/cupons":
            enviar(self.chat, self.resumo_cupons() or "Ainda não testei os cupons hoje. O teste roda uma vez por dia, a partir das 9h.")
        elif cmd == "/cupom":
            partes = arg.split()
            if not partes:
                enviar(self.chat, "Use assim: /cupom CODIGO loja (ex.: /cupom VENBA10 venba)")
                return
            codigo = partes[0].upper()
            loja = " ".join(partes[1:])
            alvo = [c for c, l in self.lojas().items() if l["tipo"] == "shopify" and (not loja or norm(loja) in norm(l["nome"]) or norm(loja) == c)]
            for c in alvo:
                if {"codigo": codigo, "loja": c} not in self.est["cupons_extra"]:
                    self.est["cupons_extra"].append({"codigo": codigo, "loja": c})
            enviar(self.chat, f"Vou testar o cupom {html.escape(codigo)} em: " + ", ".join(self.lojas()[c]["nome"] for c in alvo)
                   if alvo else "Não achei essa loja. Veja os nomes em /lojas.")
            if alvo:
                self.est["cupons_dia"] = ""  # testa já na próxima rodada
        elif cmd == "/pausar":
            self.est["pausado"] = True
            enviar(self.chat, "Avisos pausados. Use /continuar para voltar.")
        elif cmd == "/continuar":
            self.est["pausado"] = False
            enviar(self.chat, "Avisos de volta.")
        else:
            enviar(self.chat, "Não conheço esse comando.\n\n" + AJUDA)

    def adicionar_link(self, url):
        try:
            status, _, final = http(url, tentativas=2)
        except Exception:  # noqa: BLE001
            final = url
        partes = urllib.parse.urlsplit(final)
        host = partes.netloc.lower()
        chave = self.loja_do_host(host)
        nova = False
        if chave is None:
            chave, nova = self.descobrir_loja(partes)
            if chave is None:
                enviar(self.chat, f"Ainda não sei ler a loja {html.escape(host)}. Ela não usa Shopify nem WooCommerce, que são os sistemas que eu entendo.")
                return
        loja = self.lojas()[chave]
        m = re.search(r"/products/([^/?#]+)", partes.path)
        ident = m.group(1) if (loja["tipo"] == "shopify" and m) else None
        if loja["tipo"] != "shopify":
            segs = [s for s in partes.path.split("/") if s]
            ident = segs[-1] if segs else None
        prefixo = f"Loja nova adicionada: <b>{html.escape(loja['nome'])}</b>.\n" if nova else ""
        if not ident:
            enviar(self.chat, prefixo + ("Vou avisar os lançamentos das marcas que você segue." if loja["tipo"] == "shopify"
                                         else "Mande o link de um perfume dessa loja para eu vigiar."))
            return
        k = f"{chave}:{ident}"
        try:
            if loja["tipo"] == "shopify":
                st, dados, _ = http(f"{loja['url']}/products/{ident}.js")
                prod = shopify_js_para_json(json.loads(dados.decode())) if st == 200 else None
                item = resumir_shopify(prod) if prod else None
            else:
                prod = ler_woo_produto(loja["url"], ident)
                item = None
                if prod:
                    item = resumir_woo(prod, self.todas_marcas())
                    item["url"] = prod.get("permalink", final)
        except Exception:  # noqa: BLE001
            prod = None
        if not prod or not item:
            enviar(self.chat, prefixo + "Não achei esse perfume na loja. Confira se o link abre a página do produto.")
            return
        ja = k in self.vig
        self.vig.add(k)
        item.setdefault("url", "")
        item["min"] = item["p"]
        item["visto"] = agora().strftime("%Y-%m-%d")
        if k not in self.est["produtos"]:
            self.est["produtos"][k] = item
        if item["m"] and item["m"] not in self.est.get("marcas", []):
            self.est.setdefault("marcas", []).append(item["m"])
        st = "em estoque" if item["a"] else "esgotado"
        enviar(self.chat, prefixo + ("Já estava vigiando" if ja else "Agora vigiando")
               + f": <b>{html.escape(item['m'])}</b> {html.escape(item['t'])}\n{usd(item['p'])} · {st} · {html.escape(loja['nome'])}",
               foto=item.get("img") or None)

    def descobrir_loja(self, partes):
        host = partes.netloc.lower().removeprefix("www.")
        if not host:
            return None, False
        base = f"https://{partes.netloc}"
        chave = re.sub(r"[^a-z0-9]+", "", host.split(".")[0]) or "loja"
        while chave in self.lojas():
            chave += "x"
        nome = host.split(".")[0].replace("-", " ").title()
        tipo = None
        try:
            st, dados, _ = http(f"{base}/products.json?limit=1", tentativas=2)
            if st == 200 and "products" in json.loads(dados.decode()):
                tipo = "shopify"
        except Exception:  # noqa: BLE001
            pass
        if not tipo:
            segs = [s for s in partes.path.split("/") if s]
            try:
                if segs:
                    st, dados, _ = http(f"{base}/wp-json/wc/store/v1/products?slug={urllib.parse.quote(segs[-1])}", tentativas=2)
                    if st == 200 and isinstance(json.loads(dados.decode()), list):
                        tipo = "woocommerce"
            except Exception:  # noqa: BLE001
                pass
        if not tipo:
            return None, False
        self.est["lojas_extra"][chave] = {"nome": nome, "url": base, "tipo": tipo}
        return chave, True

    # ---------- cupons e descontos automáticos (1x por dia)
    def testar_cupons(self, forcar=False):
        hoje = agora().strftime("%Y-%m-%d")
        if not forcar and (self.est.get("cupons_dia") == hoje or agora().hour < int(self.cfg.get("hora_cupons", 9))):
            return
        codigos = list(self.cfg.get("cupons", [])) + list(self.est["cupons_extra"])
        for chave, loja in self.lojas().items():
            if loja["tipo"] != "shopify" or chave not in self.leituras:
                continue
            em_estoque = [(k, p) for k, p in self.est["produtos"].items()
                          if k.startswith(chave + ":") and k in self.vig and p.get("a") and p.get("var")]
            if not em_estoque:
                continue
            base = loja["url"]
            # 1) cupons: testa num frasco caro, para passar de pedidos mínimos
            k_teste, p_teste = max(em_estoque, key=lambda kp: kp[1]["p"])
            for c in [c for c in codigos if c["loja"] == chave]:
                sessao = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar.CookieJar()))
                try:
                    http(f"{base}/cart/add.js", metodo="POST", json_corpo={"items": [{"id": p_teste["var"], "quantity": 1}]}, opener=sessao)
                    http(f"{base}/cart/update.js", metodo="POST", json_corpo={"discount": c["codigo"]}, opener=sessao)
                    carrinho = json.loads(http(f"{base}/cart.js", opener=sessao)[1].decode())
                except Exception as e:  # noqa: BLE001
                    print("Teste de cupom falhou:", c["codigo"], e)
                    continue
                vale = any(d.get("applicable") and d.get("code", "").upper() == c["codigo"].upper()
                           for d in carrinho.get("discount_codes", [])) and carrinho.get("total_discount", 0) > 0
                desc = carrinho.get("total_discount", 0) / 100
                chave_c = f"{chave}:{c['codigo']}"
                ant = self.est["cupons"].get(chave_c)
                self.est["cupons"][chave_c] = {"ok": vale, "desc": desc, "teste": p_teste["p"], "dia": hoje}
                if ant is None or ant["ok"] != vale:
                    txt = (f"<b>{html.escape(c['codigo'])}</b> está valendo na {html.escape(loja['nome'])}: "
                           f"{usd(desc)} de desconto num perfume de {usd(p_teste['p'])}.") if vale else \
                          f"<b>{html.escape(c['codigo'])}</b> parou de valer na {html.escape(loja['nome'])}."
                    if ant is not None or vale:
                        self.aviso("cupom", None, None, loja, txt)
                time.sleep(1)
            # 2) descontos automáticos: um frasco de cada marca, sem cupom
            por_marca = {}
            for k, p in em_estoque:
                por_marca.setdefault(p.get("m", ""), (k, p))
            sessao = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar.CookieJar()))
            vistos = set()
            for marca, (k, p) in sorted(por_marca.items()):
                try:
                    http(f"{base}/cart/clear.js", metodo="POST", corpo=b"", opener=sessao)
                    http(f"{base}/cart/add.js", metodo="POST", json_corpo={"items": [{"id": p["var"], "quantity": 1}]}, opener=sessao)
                    carrinho = json.loads(http(f"{base}/cart.js", opener=sessao)[1].decode())
                except Exception:  # noqa: BLE001
                    continue
                desc = carrinho.get("total_discount", 0) / 100
                titulos = sorted({a["discount_application"].get("title", "") for it in carrinho.get("items", [])
                                  for a in it.get("line_level_discount_allocations", [])} |
                                 {a.get("discount_application", {}).get("title", "") for a in carrinho.get("cart_level_discount_applications", [])} - {""})
                ck = f"{chave}:{marca}"
                vistos.add(ck)
                if desc > 0:
                    novo = ck not in self.est["auto"]
                    self.est["auto"][ck] = {"titulo": ", ".join(titulos) or "automático", "valor": desc, "base": p["p"], "dia": hoje}
                    if novo:
                        self.aviso("auto", None, None, loja,
                                   f"<b>{html.escape(marca)}</b> na {html.escape(loja['nome'])}: {usd(desc)} a menos no carrinho "
                                   f"({html.escape(', '.join(titulos) or 'automático')}). Ex.: {usd(p['p'])} → <b>{usd(p['p'] - desc)}</b>")
                elif ck in self.est["auto"]:
                    self.est["auto"].pop(ck)
                    self.aviso("auto", None, None, loja, f"O desconto automático de <b>{html.escape(marca)}</b> na {html.escape(loja['nome'])} acabou.")
                time.sleep(0.5)
            try:
                http(f"{base}/cart/clear.js", metodo="POST", corpo=b"", opener=sessao)
            except Exception:  # noqa: BLE001
                pass
        self.est["cupons_dia"] = hoje

    def resumo_cupons(self):
        linhas = []
        for ck, v in sorted(self.est["cupons"].items()):
            c, codigo = ck.split(":", 1)
            nome = self.lojas().get(c, {}).get("nome", c)
            linhas.append(f"{'✅' if v['ok'] else '❌'} {html.escape(codigo)} ({html.escape(nome)})" + (f": -{usd(v['desc'])} em {usd(v['teste'])}" if v["ok"] else ""))
        for ck, v in sorted(self.est["auto"].items()):
            c, marca = ck.split(":", 1)
            nome = self.lojas().get(c, {}).get("nome", c)
            linhas.append(f"🎁 {html.escape(marca)} ({html.escape(nome)}): -{usd(v['valor'])} automático")
        return ("<b>Cupons testados</b>\n" + "\n".join(linhas)) if linhas else ""

    # ---------- site
    def preco_final(self, k, p):
        auto = self.est["auto"].get(f"{k.split(':')[0]}:{p.get('m', '')}")
        if auto and p.get("a") and auto.get("base"):
            if abs(auto["valor"] - round(auto["valor"])) < 0.01 and auto["valor"] >= 5:
                # valor fixo por frasco (ex.: Mind Games, US$ 200 a menos)
                return round(max(p["p"] - auto["valor"], 0), 2), auto["titulo"]
            taxa = auto["valor"] / auto["base"]
            return round(p["p"] * (1 - taxa), 2), auto["titulo"]
        return p["p"], ""

    def gerar_site(self):
        lojas = self.lojas()
        itens = []
        for k in sorted(self.vig):
            p = self.est["produtos"].get(k)
            if not p or "p" not in p:
                continue
            c, ident = k.split(":", 1)
            if c not in lojas:
                continue
            f, cupom = self.preco_final(k, p)
            marca = self.marca_cat.get(k) or p.get("m", "")
            img = p.get("img", "")
            if img and "cdn.shopify.com" in img:
                img += ("&" if "?" in img else "?") + "width=600"
            itens.append({"k": k, "b": marca, "t": p["t"], "s": p.get("s", ""), "x": 1 if p.get("tester") else 0,
                          "p": p["p"], "f": f, "cp": cupom, "c": p.get("c", 0), "a": 1 if p.get("a") else 0,
                          "st": lojas[c]["nome"], "u": p.get("url") or self.url_produto(c, ident), "img": img,
                          "d": p.get("visto", ""), "min": p.get("min", p["p"]), "g": chave_comparacao(marca, p)})
        todos, em_estoque = {}, {}
        for it in itens:
            todos.setdefault(it["g"], set()).add(it["st"])
            if it["a"] and it["p"] != 999:
                em_estoque.setdefault(it["g"], []).append(it["f"])
        for it in itens:
            menor = min(em_estoque.get(it["g"], []), default=None)
            if len(todos[it["g"]]) < 2:
                it["l"] = "Só nesta oferta"
            elif not it["a"] or menor is None:
                it["l"] = "Esgotado aqui"
            elif it["f"] <= menor + 0.001:
                it["l"] = "Melhor preço"
            else:
                it["l"] = f"+{round((it['f'] / menor - 1) * 100)}% acima"
        itens.sort(key=lambda x: (norm(x["b"]), x["f"], norm(x["t"])))
        cupons = [{"codigo": ck.split(":", 1)[1], "loja": lojas.get(ck.split(":")[0], {}).get("nome", ""), "desc": v["desc"]}
                  for ck, v in sorted(self.est["cupons"].items()) if v.get("ok")]
        dados = {"cotacao": round(self.cot(), 2), "cupons": cupons, "produtos": itens}
        h = hashlib.sha1(json.dumps(dados, sort_keys=True).encode()).hexdigest()
        if h != self.est.get("site_hash") or not os.path.exists(ARQ_DADOS_SITE):
            dados["atualizado"] = agora().strftime("%d/%m/%Y %H:%M")
            salvar_json(ARQ_DADOS_SITE, dados, compacto=True)
            self.est["site_hash"] = h

    # ---------- rodada
    def rodar(self, forcar_cupons=False):
        self.ler_lojas()
        if "marcas" not in self.est:
            ms = set()
            for chave, prods in self.leituras.items():
                for h, p in prods.items():
                    if f"{chave}:{h}" in self.vig and p.get("vendor"):
                        ms.add(p["vendor"])
            self.est["marcas"] = sorted(ms, key=norm)
        self.tratar_telegram()
        self.comparar()
        try:
            self.testar_cupons(forcar=forcar_cupons)
        except Exception as e:  # noqa: BLE001
            print("Teste de cupons falhou:", e)
        self.enviar_avisos()
        hoje = agora().strftime("%Y-%m-%d")
        if self.chat and not self.est.get("pausado"):
            for chave, n in self.est["falhas"].items():
                if n >= 3 and self.est.get("aviso_falha", {}).get(chave) != hoje and chave in self.lojas():
                    enviar(self.chat, f"⚠️ Não consigo ler a {self.lojas()[chave]['nome']} há {n} verificações seguidas.")
                    self.est.setdefault("aviso_falha", {})[chave] = hoje
        self.est["ultima"] = agora().strftime("%d/%m %H:%M")
        self.est["batimento"] = agora().strftime("%G-%V")
        self.est["vigiados"] = sorted(self.vig)
        self.gerar_site()
        salvar_json(ARQ_ESTADO, self.est)
        print(f"Lojas lidas: {', '.join(self.leituras) or 'nenhuma'}. Vigiando {len(self.vig)}. Avisos: {len(self.avisos)}.")
        return self.avisos


CONC = [(r"\b(extrait de parf?um|extrait)\b", "extrait"), (r"\b(eau de parfum|edp)\b", "edp"),
        (r"\b(eau de toilette|edt)\b", "edt"), (r"\b(absolu de parfum|pure parfum|parfum)\b", "parfum")]


def chave_comparacao(marca, p):
    """Mesmo perfume, tamanho e tipo em lojas diferentes têm a mesma chave."""
    x = norm(p.get("t", ""))
    conc = ""
    for padrao, nome in CONC:
        if re.search(padrao, x):
            conc = conc or nome
            x = re.sub(padrao, " ", x)
    x = re.sub(r"\b\d+\s*ml\b", " ", x)
    tk = [w for w in x.split() if w not in ("man", "woman", "men", "women", "perfume", "the", "no", "n", "de", "du", "di", "tester", "ml")]
    return "|".join([norm(marca), " ".join(sorted(tk)), p.get("s", ""), "T" if p.get("tester") else "R", conc])


if __name__ == "__main__":
    if not TOKEN and not DRY_RUN:
        sys.exit("Falta o segredo TELEGRAM_TOKEN.")
    Vigia().rodar()
