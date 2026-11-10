"""Vigia de perfumes: lê os catálogos das lojas, compara com a última leitura
e avisa no Telegram quando algo volta ao estoque, baixa de preço, ganha
desconto ou quando uma marca seguida lança um perfume novo.

Roda a cada 30 minutos pelo GitHub Actions. Precisa do segredo TELEGRAM_TOKEN.
Para testar sem enviar mensagens: DRY_RUN=1 python vigia.py
"""
import datetime
import html
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

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

PEQUENO = re.compile(r"sample|decant|atomi[sz]er|travel|discovery|amostra", re.I)
ML = re.compile(r"(\d+(?:[.,]\d+)?)\s*ml", re.I)


# ---------------------------------------------------------------- utilidades
def ler_json(caminho, padrao):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return padrao


def salvar_json(caminho, dados):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, caminho)


def norm(texto):
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def baixar_json(url, tentativas=3, dados=None):
    for n in range(tentativas):
        try:
            req = urllib.request.Request(url, data=dados, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (n + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(3 * (n + 1))
    raise RuntimeError("sem resposta de " + url)


def usd(v):
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return "US$ " + s


def brl(v):
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return "R$ " + s


# ---------------------------------------------------------------- lojas
def ler_loja(base, max_paginas=80):
    """Catálogo inteiro de uma loja Shopify, pela lista pública de produtos."""
    produtos = []
    for pagina in range(1, max_paginas + 1):
        dados = baixar_json(f"{base}/products.json?limit=250&page={pagina}")
        lote = dados.get("products", [])
        if not lote:
            break
        produtos.extend(lote)
        time.sleep(1)
    if not produtos:
        raise RuntimeError("catálogo vazio")
    return produtos


def variante_pequena(titulo):
    if PEQUENO.search(titulo or ""):
        return True
    m = ML.search(titulo or "")
    return bool(m) and float(m.group(1).replace(",", ".")) <= 10


def resumir(prod):
    """Reduz um produto ao frasco principal: o maior disponível, ou o maior de todos."""
    vs = [v for v in prod.get("variants", []) if not variante_pequena(v.get("title"))] or prod.get("variants", [])
    if not vs:
        return None
    vs = sorted(vs, key=lambda v: float(v.get("price") or 0), reverse=True)
    disp = [v for v in vs if v.get("available")]
    v = disp[0] if disp else vs[0]
    preco = float(v.get("price") or 0)
    cheio = float(v.get("compare_at_price") or 0)
    tam = ML.search(v.get("title") or "")
    titulo, marca = prod.get("title", ""), prod.get("vendor", "")
    if marca and titulo.lower().startswith(marca.lower()):
        titulo = titulo[len(marca):].lstrip(" -–:") or titulo
    return {
        "t": titulo,
        "m": prod.get("vendor", ""),
        "p": preco,
        "c": cheio if cheio > preco else 0,
        "a": bool(disp),
        "s": (tam.group(1) + " ml") if tam else "",
        "tester": "tester" in (v.get("title", "") + " " + prod.get("title", "")).lower(),
    }


# ---------------------------------------------------------------- telegram
def tg(metodo, **params):
    if DRY_RUN or not TOKEN:
        return {"ok": True, "result": []}
    url = f"https://api.telegram.org/bot{TOKEN}/{metodo}"
    corpo = urllib.parse.urlencode(params).encode()
    try:
        return baixar_json(url, dados=corpo)
    except Exception as e:  # noqa: BLE001
        print("Telegram falhou:", metodo, type(e).__name__)
        return {"ok": False, "result": []}


ENVIADAS = []


def enviar(chat_id, texto):
    ENVIADAS.append(texto)
    if DRY_RUN:
        print("---- mensagem ----\n" + re.sub(r"<[^>]+>", "", texto))
        return
    if chat_id:
        tg("sendMessage", chat_id=chat_id, text=texto, parse_mode="HTML", disable_web_page_preview="true")
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
    "/marcas: marcas seguidas para lançamentos\n"
    "/seguir nome: seguir uma marca (ex.: /seguir xerjoff)\n"
    "/parar nome: deixar de seguir uma marca\n"
    "/pausar e /continuar: silenciar ou voltar os avisos\n\n"
    "Eu verifico as lojas a cada 30 minutos, e os comandos são lidos na verificação seguinte."
)


def tratar_comandos(estado, vendors):
    r = tg("getUpdates", offset=estado.get("offset", 0), timeout=0)
    for upd in r.get("result", []):
        estado["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        texto = (msg.get("text") or "").strip()
        if not chat or not texto.startswith("/"):
            continue
        if estado.get("chat_id") is None and texto.startswith("/start"):
            estado["chat_id"] = chat
            enviar(chat, "Pronto! Vou te avisar aqui quando um perfume voltar ao estoque, baixar de preço ou ganhar desconto.\n\n" + AJUDA)
            continue
        if chat != estado.get("chat_id"):
            continue  # só obedece ao dono
        cmd, _, arg = texto.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        seguidas = set(estado.setdefault("marcas", []))
        if cmd in ("/ajuda", "/help", "/start"):
            enviar(chat, AJUDA)
        elif cmd == "/status":
            vig = estado.get("produtos", {})
            n_ok = sum(1 for p in vig.values() if p.get("a"))
            enviar(chat, f"Vigiando <b>{len(vig)}</b> perfumes ({n_ok} em estoque) e {len(seguidas)} marcas para lançamentos.\n"
                         f"Última verificação: {estado.get('ultima', '-')} (horário de Brasília)."
                         + ("\n\nAvisos pausados. Use /continuar." if estado.get("pausado") else ""))
        elif cmd == "/marcas":
            enviar_em_partes(chat, "<b>Marcas seguidas</b>", [html.escape(", ".join(sorted(seguidas, key=norm)))])
        elif cmd in ("/seguir", "/parar"):
            if not arg:
                enviar(chat, f"Diga a marca, por exemplo: {cmd} xerjoff")
                continue
            achadas = sorted({v for v in vendors if norm(arg) in norm(v)}) or [arg]
            if cmd == "/seguir":
                seguidas.update(achadas)
                enviar(chat, "Seguindo: " + html.escape(", ".join(achadas)))
            else:
                alvo = {s for s in seguidas if norm(arg) in norm(s)}
                seguidas -= alvo
                enviar(chat, ("Deixei de seguir: " + html.escape(", ".join(sorted(alvo)))) if alvo else "Não achei essa marca na lista.")
            estado["marcas"] = sorted(seguidas, key=norm)
        elif cmd == "/pausar":
            estado["pausado"] = True
            enviar(chat, "Avisos pausados. Use /continuar para voltar.")
        elif cmd == "/continuar":
            estado["pausado"] = False
            enviar(chat, "Avisos de volta.")


# ---------------------------------------------------------------- principal
def linha(item, loja_nome, url, extra, cot):
    nome = f"<b>{html.escape(item['m'])}</b> {html.escape(item['t'])}"
    det = " · ".join(x for x in (item.get("s"), "tester" if item.get("tester") else "") if x)
    if det:
        nome += f" ({det})"
    return f"{nome}\n{extra} · {brl(item['p'] * cot)}\n<a href=\"{url}\">Ver na {html.escape(loja_nome)}</a>"


def cotacao(cfg):
    try:
        d = baixar_json("https://economia.awesomeapi.com.br/json/last/USD-BRL", tentativas=2)
        return float(d["USDBRL"]["bid"])
    except Exception:  # noqa: BLE001
        return float(cfg.get("cotacao_padrao", 5.5))


def main(lojas_falsas=None):
    cfg = ler_json(ARQ_CONFIG, {})
    queda = float(cfg.get("queda_minima", 0.05))
    lojas = cfg.get("lojas", {})
    catalogo = ler_json(ARQ_CATALOGO, {"produtos": []})["produtos"]
    estado = ler_json(ARQ_ESTADO, {})
    estado.setdefault("produtos", {})
    estado.setdefault("conhecidos", {})
    estado.setdefault("falhas", {})
    vigiados = estado.setdefault("vigiados", sorted({f"{p['loja']}:{p['handle']}" for p in catalogo}))
    vig_set = set(vigiados)
    cot = cotacao(cfg)

    leituras = {}
    for chave, loja in lojas.items():
        try:
            prods = lojas_falsas[chave] if lojas_falsas is not None else ler_loja(loja["url"])
            antes = len(estado["conhecidos"].get(chave, []))
            if antes and len(prods) < antes * 0.7:
                raise RuntimeError(f"catálogo veio incompleto ({len(prods)} de {antes})")
            leituras[chave] = {p["handle"]: p for p in prods}
            estado["falhas"][chave] = 0
        except Exception as e:  # noqa: BLE001
            print(f"Falha ao ler {loja['nome']}: {type(e).__name__}: {e}")
            estado["falhas"][chave] = estado["falhas"].get(chave, 0) + 1

    vendors = sorted({p.get("vendor", "") for l in leituras.values() for p in l.values() if p.get("vendor")})
    primeira_vez = "marcas" not in estado
    if primeira_vez:
        # marcas seguidas = marcas dos perfumes vigiados, com o nome que a loja usa
        ms = set()
        for chave, l in leituras.items():
            for h, p in l.items():
                if f"{chave}:{h}" in vig_set and p.get("vendor"):
                    ms.add(p["vendor"])
        estado["marcas"] = sorted(ms, key=norm)

    tratar_comandos(estado, vendors)
    chat = estado.get("chat_id")
    seguidas = {norm(m) for m in estado.get("marcas", [])}

    avisos = {"estoque": [], "preco": [], "desconto": [], "lancamento": []}
    for chave, l in leituras.items():
        loja = lojas[chave]
        conhecidos = set(estado["conhecidos"].get(chave, []))
        ja_lido = chave in estado["conhecidos"]
        # muitos "novos" de uma vez costuma ser reorganização da loja, não lançamento
        avisar_novos = len(set(l) - conhecidos) <= 40
        for h, prod in l.items():
            k = f"{chave}:{h}"
            item = resumir(prod)
            if item is None:
                continue
            url = f"{loja['url']}/products/{h}"
            novo_na_loja = ja_lido and h not in conhecidos
            if novo_na_loja and norm(item["m"]) in seguidas and k not in vig_set:
                vigiados.append(k)
                vig_set.add(k)
                if not avisar_novos:
                    continue
                status = "em estoque" if item["a"] else "ainda esgotado"
                avisos["lancamento"].append(linha(item, loja["nome"], url, f"Novo na loja, {status}: {usd(item['p'])}", cot))
            if k not in vig_set:
                continue
            ant = estado["produtos"].get(k)
            if ant is not None:
                if not ant["a"] and item["a"]:
                    avisos["estoque"].append(linha(item, loja["nome"], url, f"Voltou ao estoque por {usd(item['p'])}", cot))
                elif item["a"] and item["p"] > 0:
                    menor = ant.get("min", ant["p"])
                    caiu = item["p"] <= ant["p"] * (1 - queda)
                    recorde = item["p"] < menor - 0.009
                    if caiu or recorde:
                        txt = f"{usd(ant['p'])} → <b>{usd(item['p'])}</b>"
                        if item["p"] < ant["p"]:
                            txt += f" (-{round((1 - item['p'] / ant['p']) * 100)}%)"
                        if recorde:
                            txt += ", menor preço já visto"
                        avisos["preco"].append(linha(item, loja["nome"], url, txt, cot))
                    elif item["c"] and (not ant.get("c") or item["c"] - item["p"] > ant["c"] - ant["p"] + 0.5):
                        off = round((1 - item["p"] / item["c"]) * 100)
                        avisos["desconto"].append(linha(item, loja["nome"], url, f"De {usd(item['c'])} por <b>{usd(item['p'])}</b> (-{off}%)", cot))
                item["min"] = min(ant.get("min", ant["p"]), item["p"]) if item["p"] > 0 else ant.get("min", 0)
            else:
                item["min"] = item["p"]
            estado["produtos"][k] = item
        estado["conhecidos"][chave] = sorted(set(l))

    agora = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3)))
    estado["ultima"] = agora.strftime("%d/%m %H:%M")
    estado["batimento"] = agora.strftime("%G-%V")  # muda toda semana e mantém o GitHub ativo
    estado["vigiados"] = sorted(vig_set)

    if chat and not estado.get("pausado"):
        titulos = {"estoque": "🟢 <b>Voltou ao estoque</b>", "preco": "🔻 <b>Baixou de preço</b>",
                   "desconto": "🏷️ <b>Desconto novo</b>", "lancamento": "✨ <b>Lançamento de marca seguida</b>"}
        for tipo, linhas_ in avisos.items():
            if linhas_:
                enviar_em_partes(chat, titulos[tipo], linhas_)
        hoje = agora.strftime("%Y-%m-%d")
        for chave, n in estado["falhas"].items():
            if n >= 3 and estado.get("aviso_falha", {}).get(chave) != hoje:
                enviar(chat, f"⚠️ Não consigo ler a {lojas[chave]['nome']} há {n} verificações seguidas.")
                estado.setdefault("aviso_falha", {})[chave] = hoje
    salvar_json(ARQ_ESTADO, estado)
    total = sum(len(v) for v in avisos.values())
    print(f"Lojas lidas: {', '.join(leituras) or 'nenhuma'}. Vigiando {len(vig_set)}. Avisos: {total}.")
    return avisos


if __name__ == "__main__":
    if not TOKEN and not DRY_RUN:
        sys.exit("Falta o segredo TELEGRAM_TOKEN.")
    main()
