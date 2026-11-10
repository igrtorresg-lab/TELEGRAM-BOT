"""
Bot de stock -> Telegram (autodescubre colecciones de One Piece y preventas).

Para cada tienda Shopify:
  1) lee /collections.json y detecta dos tipos de colecciones:
       - de One Piece (cajas, sobres, decks, premium, illustration box...):
         vigila TODOS sus productos
       - de preventa (mezclan varios juegos): vigila solo los productos
         One Piece / OP / ST / EB / PRB (incluye ST-38)
  2) lee los productos de esas colecciones
  3) te avisa por Telegram, con foto y link, cuando uno pasa a tener stock

Variables de entorno requeridas: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = Path("state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# --- Tiendas (una por línea, SIEMPRE con coma al final) -------------------
STORES = [
    "https://oasisgames.cl",
    "https://drawn.cl",
    "https://tiendalacomarca.cl",
    "https://metagaming.cl",
    "https://filecity.cl",
    "https://mrloc.cl",
    "https://elreinodelosduelos.cl",
    "https://tcglifestore.cl",
    "https://thirdimpact.cl",
    "https://royalcardstore.cl",
    "https://deckkingdom.cl",
    "https://piedrabruja.cl",
    "https://mirax.cl",
    "https://oneupstore.cl",
    "https://metagame.cl",
    "https://progaming.cl",
    "https://tcglifestore.cl/",
    "https://cardhousegaming.com/",
    "https://bastiongoblin.com",
    "https://tecitogames.com",
    "https://tablecatgames.cl",
    "https://dyskeu.cl",
]

# Tiendas que NO son Shopify: se leen desde el HTML de sus categorías.
# type "tiendanube" (File City). Cada ruta: (ruta, solo_titulos_one_piece)
#   False = toda la categoría es One Piece | True = categoría mezclada (preventas): se filtra por nombre
HTML_STORES = {
    "https://filecity.cl": {
        "type": "tiendanube",
        "paths": [
            ("/one-piece-card-game/", False),
            ("/pre-ventas1/", True),
        ],
    },
}

# Colecciones extra que quieras forzar (URL completa). Se vigilan TODOS sus productos.
EXTRA_COLLECTIONS = [
    # "https://oasisgames.cl/collections/one-piece-sealed",
]

# Una colección cuenta como "One Piece" si su nombre/handle coincide con esto
COLLECTION_RE = re.compile(
    r"one[-\s]?piece|(^|[-\s])op([-\s]|$)|illustration[-\s]box|"
    r"premium[-\s](extra[-\s])?booster|extra[-\s]booster",
    re.IGNORECASE,
)

# Colecciones de preventa (mezclan juegos: solo se guardan productos One Piece)
PREVENTA_RE = re.compile(r"pre[-\s]?venta|pre[-\s]?order", re.IGNORECASE)

# Nombres de producto que interesan dentro de las colecciones de preventa
# (st-?\d cubre ST-38 y cualquier otro starter deck)
TITLE_RE = re.compile(
    r"one[-\s]?piece|\b(op|eb|prb|st)-?\d{1,2}\b|illustration[-\s]box|"
    r"premium[-\s](extra[-\s])?booster|extra[-\s]booster",
    re.IGNORECASE,
)

# Se excluyen cartas sueltas y accesorios: solo interesa el producto sellado
INCLUDE_SINGLES = False
SINGLES_RE = re.compile(
    r"single|suelta|accesorio|sleeve|playmat|protector|deck-?box|carpeta|album",
    re.IGNORECASE,
)


# --- Filtros por PRODUCTO (se aplican dentro de cada colección) -----------
# Carta suelta: código de carta tipo OP09-118, EB01-006, ST05-002, PRB01-001
SINGLE_CARD_RE = re.compile(r"(?<![a-z])(op|eb|prb|st)-?\d{2}-\d{2,3}", re.IGNORECASE)

# Accesorios, otros juegos, promos/variantes y productos de prueba
UNWANTED_RE = re.compile(
    r"sleeve|playmat|protector|porta[-\s]?mazo|deck[-\s]?box|(^|[-\s])dice([-\s]|$)|"
    r"lego|paper[-\s]theater|tournament|prueba|"
    r"gundam|digimon|pok[eé]mon|(^|[-\s])magic([-\s]|$)|mtg|riftbound|dragon[-\s]?ball|"
    r"hololive|yu[-\s]?gi[-\s]?oh|"
    r"iron[-\s]blooded|eternal[-\s]nexus|g[-\s]generation|aquatic[-\s]assault|"
    r"heavy[-\s]dominion|flash[-\s]of[-\s]radiance|amethyst[-\s]mandala|data[-\s]squad|"
    r"unicorn|cosmic[-\s]crisis|destiny[-\s]ignition|generational[-\s]pulse|"
    r"promotion[-\s]cards|(^|[-\s])op[-\s]pr([-\s]|$)|jolly[-\s]roger[-\s]foil|"
    r"super[-\s]pre[-\s]release|pre[-\s]release[-\s]cards|alternate[-\s]art|parallel|manga|"
    r"pirate[-\s]foil|full[-\s]art|bandai[-\s]card[-\s]games[-\s]fest|(^|[-\s])p[-\s]?\d{3}([-\s]|$)|"
    r"^[a-z]+(-[a-z]+)?-premium-card-collection",   # carta promo: "izo-premium-card-collection..."
    re.IGNORECASE,
)

# Identidad One Piece: el producto DEBE decir "one piece" o llevar un código OP/EB/PRB/ST/IB/DP/TS/SD...
ONE_PIECE_RE = re.compile(
    r"one[-\s]?piece|(^|-)op-|(^|[^a-z])(op|eb|prb|st|ib|dp|ts|sd|ln|lt|df|peb)-?\d{1,2}([^0-9a-z]|$)",
    re.IGNORECASE,
)


def is_unwanted(p):
    """True si el producto es carta suelta, accesorio u otro juego."""
    handle = p["handle"].lower()
    text = f"{handle} {p['title'].lower()}"
    if not ONE_PIECE_RE.search(text):
        return True                                  # no es de One Piece (Pokémon, Dragon Ball...)
    return bool(
        UNWANTED_RE.search(text)
        or SINGLE_CARD_RE.search(text)
        or re.search(r"-\d{6}$", handle)          # cartas sueltas de La Comarca
        or re.search(r"(^|-)don(-|$)", handle)      # cartas DON!!
    )


def get_json(url, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def discover_collections(base):
    """Devuelve [(handle, solo_titulos_one_piece)]."""
    cols, page = [], 1
    while True:
        batch = get_json(f"{base}/collections.json", limit=250, page=page)["collections"]
        cols += batch
        if len(batch) < 250:
            break
        page += 1
        time.sleep(0.5)
    found = []
    for c in cols:
        text = f"{c['handle']} {c.get('title', '')}"
        if c.get("products_count", 1) == 0:
            continue
        if not INCLUDE_SINGLES and SINGLES_RE.search(text):
            continue
        if COLLECTION_RE.search(text):
            found.append((c["handle"], False))
        elif PREVENTA_RE.search(text):
            found.append((c["handle"], True))
    return found


def fetch_products(col_url):
    products, page = [], 1
    while True:
        batch = get_json(f"{col_url}/products.json", limit=250, page=page)["products"]
        products += batch
        if len(batch) < 250:
            break
        page += 1
        time.sleep(0.5)
    return products


def scan_store(base):
    """Devuelve {link: {title, available, price, image, preorder}} de la tienda."""
    if base in HTML_STORES:
        return scan_html_store(base, HTML_STORES[base])
    targets = [(f"{base}/collections/{h}", only_title, h) for h, only_title in discover_collections(base)]
    targets += [(u.rstrip("/"), False, u) for u in EXTRA_COLLECTIONS if u.startswith(base)]
    found = {}
    for col_url, only_title, handle in targets:
        for p in fetch_products(col_url):
            if is_unwanted(p):
                continue
            if only_title and not TITLE_RE.search(p["title"]):
                continue
            link = f"{base}/products/{p['handle']}"
            prices = [float(v["price"]) for v in p["variants"] if v.get("available")]
            found[link] = {
                "title": p["title"],
                "available": bool(prices),
                "price": min(prices) if prices else None,
                "image": p["images"][0]["src"] if p.get("images") else None,
                "preorder": bool(PREVENTA_RE.search(handle) or PREVENTA_RE.search(p["title"])),
            }
        time.sleep(0.5)
    print(f"{base}: {len(targets)} colecciones, {len(found)} productos")
    return found


# --- Lectura de tiendas Tiendanube (HTML) -----------------------------------
def _slug_of(a):
    m = re.search(r"/productos/([^/?#]+)", a.get("href") or "")
    return m.group(1) if m else None


def _price_clp(text):
    vals = [int(x.replace(".", "")) for x in re.findall(r"\$\s?(\d{1,3}(?:\.\d{3})+|\d+)", text)]
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else None


def _img_url(card, base):
    img = card.find("img")
    if not img:
        return None
    for attr in ("data-srcset", "srcset", "data-src", "src"):
        val = img.get(attr)
        if not val:
            continue
        url = val.split(",")[0].strip().split(" ")[0]
        if url.startswith("data:"):
            continue
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return base + url
        return url
    return None


def parse_tiendanube(html_text, base):
    """Devuelve {slug: {title, available, price, image}} de una página de categoría."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError("falta beautifulsoup4 (pip install beautifulsoup4)") from e
    soup = BeautifulSoup(html_text, "html.parser")
    items = {}
    for a in soup.find_all("a", href=True):
        slug = _slug_of(a)
        if not slug or slug in items:
            continue
        # la "tarjeta" es el elemento más grande que contiene solo este producto
        card, levels = a, 0
        while card.parent is not None and card.parent.name not in ("body", "html", "[document]") and levels < 8:
            slugs = {_slug_of(x) for x in card.parent.find_all("a", href=True)} - {None}
            if len(slugs) > 1:
                break
            card, levels = card.parent, levels + 1
        text = card.get_text(" ", strip=True)
        title = next((x["title"] for x in card.find_all("a", href=True)
                      if _slug_of(x) == slug and x.get("title")), "")
        if not title:
            img = card.find("img", alt=True)
            title = (img["alt"] if img and img["alt"] else "").strip()
        if not title:
            title = max((x.get_text(" ", strip=True) for x in card.find_all("a", href=True)
                         if _slug_of(x) == slug), key=len, default=slug)
        items[slug] = {
            "title": title,
            "available": not re.search(r"\bagotado\b|sin stock", text, re.IGNORECASE),
            "price": _price_clp(text),
            "image": _img_url(card, base),
        }
    return items


def scan_html_store(base, cfg):
    found = {}
    for path, only_title in cfg["paths"]:
        seen = set()
        for page in range(1, 11):
            r = requests.get(base + path, params={"page": page} if page > 1 else None,
                             headers=HEADERS, timeout=30)
            if r.status_code == 404 and page > 1:
                break
            r.raise_for_status()
            items = parse_tiendanube(r.text, base)
            new = [sl for sl in items if sl not in seen]
            if not new:
                break
            seen.update(new)
            for sl in new:
                it = items[sl]
                # se filtra solo por el título: el slug de Tiendanube lleva un sufijo aleatorio
                # (ej. "-18o84") que podría confundirse con un código de carta
                if is_unwanted({"handle": "", "title": it["title"]}):
                    continue
                if only_title and not TITLE_RE.search(it["title"]):
                    continue
                found[f"{base}/productos/{sl}/"] = {
                    "title": it["title"],
                    "available": it["available"],
                    "price": it["price"],
                    "image": it["image"],
                    "preorder": bool(PREVENTA_RE.search(path) or PREVENTA_RE.search(it["title"])),
                }
            time.sleep(0.5)
    print(f"{base}: {len(cfg['paths'])} categorías (HTML), {len(found)} productos")
    return found


# --- Diagnóstico: detecta la plataforma de cada tienda y sus enlaces One Piece ----
PLATFORM_MARKS = [
    ("tiendanube", r"mitiendanube|nuvemshop|tiendanube"),
    ("shopify", r"cdn\.shopify|myshopify"),
    ("jumpseller", r"jumpseller"),
    ("woocommerce", r"woocommerce|wp-content"),
    ("magento", r"magento|mage-"),
    ("prestashop", r"prestashop"),
    ("wix", r"wixstatic"),
    ("vtex", r"vtex"),
    ("bsale", r"bsale"),
]


def diagnostico(urls):
    for base in urls:
        try:
            r = requests.get(base, headers=HEADERS, timeout=30)
        except Exception as e:  # noqa: BLE001
            print(f"DIAG {base}: error {e}")
            continue
        page = r.text
        plat = [n for n, pat in PLATFORM_MARKS if re.search(pat, page, re.IGNORECASE)]
        hits = []
        for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
            label = re.sub(r"<[^>]+>|\s+", " ", label).strip()
            if re.search(r"one[-\s]?piece|(^|[-/\s])op-?\d", href + " " + label, re.IGNORECASE):
                if href not in [h for h, _ in hits]:
                    hits.append((href, label[:50]))
        print(f"DIAG {base}: HTTP {r.status_code} | plataforma={plat or '?'} | enlaces OP={hits[:10]}")


def safe_scan(base):
    try:
        return base, scan_store(base), None
    except Exception as e:  # noqa: BLE001
        return base, None, e


def telegram(text, image=None):
    if image:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
            json={"chat_id": CHAT_ID, "photo": image, "caption": text, "parse_mode": "HTML"},
            timeout=30,
        )
        if r.ok:
            return
        print("sendPhoto falló, envío solo texto:", r.status_code, r.text)
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    if not r.ok:
        print("Telegram error:", r.status_code, r.text)
    r.raise_for_status()


def enviar_lista(results):
    """Manda a Telegram la lista de productos con stock, agrupada por tienda."""
    lines, total = [], 0
    for base, found, err in results:
        if err is not None or not found:
            continue
        en_stock = [(l, p) for l, p in found.items() if p["available"]]
        if not en_stock:
            continue
        total += len(en_stock)
        lines.append(f"\n<b>{html.escape(base.replace('https://', ''))}</b>")
        for link, p in sorted(en_stock, key=lambda x: x[1]["title"].lower()):
            price = f"${p['price']:,.0f}".replace(",", ".")
            pre = " ⏳" if p["preorder"] else ""
            lines.append(f'• <a href="{link}">{html.escape(p["title"])}</a> — {price}{pre}')
    if not lines:
        telegram("📋 Ahora mismo no hay productos con stock en las tiendas vigiladas.")
        return
    header = f"📋 <b>Productos con stock ahora: {total}</b>  (⏳ = preventa)"
    messages, cur = [], header
    for line in lines:
        if len(cur) + len(line) + 1 > 3800:
            messages.append(cur)
            cur = ""
        cur += "\n" + line
    messages.append(cur)
    for m in messages:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": m, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=30,
        )
        if not r.ok:
            print("Telegram error:", r.status_code, r.text)
        r.raise_for_status()
        time.sleep(1)


def main():
    if os.environ.get("DIAG", "").lower() == "true":
        diagnostico(STORES)
        return
    first_run = not STATE_FILE.exists()
    old = {} if first_run else json.loads(STATE_FILE.read_text())
    new, alerts, failed = {}, [], 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(safe_scan, STORES))

    for base, found, err in results:
        if err is not None:
            print(f"ERROR {base}: {err}", file=sys.stderr)
            failed += 1
            new.update({k: v for k, v in old.items() if k.startswith(base + "/products/")})
            continue
        for link, p in found.items():
            new[link] = p["available"]
            if p["available"] and not old.get(link, False) and not first_run:
                price = f"${p['price']:,.0f}".replace(",", ".")
                tag = "⏳ <b>Preventa</b>\n" if p["preorder"] else ""
                alerts.append((
                    f"🟢 <b>{html.escape(p['title'])}</b>\n"
                    f"{tag}"
                    f"Desde {price} CLP\n"
                    f'<a href="{link}">Ver producto</a>',
                    p["image"],
                ))

    for msg, image in alerts:
        telegram(msg, image)
        time.sleep(1)

    if first_run:
        telegram(
            f"✅ Bot iniciado. Vigilando {len(new)} productos "
            f"({sum(new.values())} con stock ahora) en {len(STORES) - failed} tiendas."
        )

    if os.environ.get("LISTAR", "").lower() == "true":
        enviar_lista(results)

    STATE_FILE.write_text(json.dumps(new, indent=1, sort_keys=True))
    sys.exit(1 if failed == len(STORES) else 0)


if __name__ == "__main__":
    main()
