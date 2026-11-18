"""
Bot de stock -> Telegram (autodescubre colecciones de One Piece y preventas).

Para cada tienda detecta la plataforma automáticamente:
  - Shopify      -> /collections.json + /products.json
  - WooCommerce  -> Store API pública (/wp-json/wc/store/v1/...)
  - HTML_STORES  -> lectura del HTML de categorías, según HTML_PRESETS
                    (Tiendanube, Bsale, Jumpseller, WooCommerce sin API, PrestaShop)
  - Otra / bloqueada -> se omite con un aviso (no rompe el run ni borra su estado)

En cada tienda vigila:
  - categorías/colecciones de One Piece: TODOS sus productos (sellados)
  - categorías de preventa: solo los productos One Piece / OP / ST / EB / PRB
y te avisa por Telegram, con foto y link, cuando uno pasa a tener stock.

Variables de entorno requeridas: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
  TELEGRAM_CHAT_ID admite VARIOS chats separados por coma (tú, un grupo, un amigo):
  por ejemplo  -5415617768,7544062179
Opcionales: DIAG=true (solo diagnóstico de plataformas), LISTAR=true (envía la lista con stock)
"""
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# Varios chats separados por coma: "-5415617768,7544062179"
CHAT_IDS = [c.strip() for c in CHAT_ID.split(",") if c.strip()]
STATE_FILE = Path("state.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.5",
}


def fetch(url, params=None, timeout=30, tries=3):
    """GET con reintentos: cortes de conexión (IncompleteRead) y errores 429/5xx suelen ser pasajeros."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(3 * (i + 1))
                continue
            return r
        except requests.RequestException as e:   # incluye ChunkedEncodingError, Timeout, ConnectionError
            last = e
            if i < tries - 1:
                time.sleep(3 * (i + 1))
    raise last

# --- Tiendas (una por línea, SIEMPRE con coma al final) -------------------
# Da igual si terminan en "/" o si se repiten: se normalizan abajo.
STORES = [
    "https://oasisgames.cl",
    "https://drawn.cl",
    "https://tiendalacomarca.cl",
    "https://metagaming.cl",
    "https://filecity.cl",
    "https://mrloc.cl",
    "https://clubdelgoblin.com",
    "https://elreinodelosduelos.cl",
    "https://tcglifestore.cl",
    "https://thirdimpact.cl",
    "https://royalcardstore.cl",
    "https://deckkingdom.cl",
    "https://piedrabruja.cl",
    "https://oneupstore.cl",
    "https://metagame.cl",
    "https://progaming.cl",
    "https://bastiongoblin.com",
    "https://tecitogames.com",
    "https://pokedals.cl",
    "https://tablecatgames.cl",
    "https://dyskeu.cl",
    "https://cardhousegaming.com",
    "https://tcglab.cl",
    "https://www.wargaming.cl",
]
STORES = list(dict.fromkeys(s.strip().rstrip("/") for s in STORES if s.strip()))

# Cómo reconocer un producto en el HTML de cada plataforma
#   product_re:    regex sobre la ruta del link; el grupo 1 identifica al producto
#   page_param:    parámetro de paginación de la plataforma
#   require_price: solo cuenta como producto si su tarjeta muestra un precio
#                  (Jumpseller no usa prefijo en sus URLs; sin esto, el menú parecería producto)
#   trailing_slash: el link guardado termina en "/" (mantiene compatible el state.json de File City)
HTML_PRESETS = {
    "tiendanube": {"product_re": r"^/productos/([^/?#]+)", "page_param": "page", "trailing_slash": True},
    "bsale": {"product_re": r"^/product/([^/?#]+)", "page_param": "page"},
    "jumpseller": {"product_re": r"^/([a-z0-9][a-z0-9-]*)/?$", "page_param": "page", "require_price": True},
    "woocommerce": {"product_re": r"^/(?:producto|product)/([^/?#]+)", "page_param": "paged"},
    "prestashop": {"product_re": r"/(\d+-[^/?#]+)\.html$", "page_param": "page"},
}

# Tiendas que se leen desde el HTML de sus categorías (no tienen API pública).
#   platform: una de HTML_PRESETS
#   paths: (ruta_categoria, solo_titulos_one_piece)
#       False = toda la categoría es One Piece | True = categoría mezclada (preventas)
HTML_STORES = {
    "https://filecity.cl": {
        "platform": "tiendanube",
        "paths": [
            ("/one-piece-card-game/", False),
            ("/pre-ventas1/", True),
        ],
    },
    "https://mrloc.cl": {
        "platform": "jumpseller",
        "paths": [("/one-piece-tcg", False)],
    },
    "https://elreinodelosduelos.cl": {
        "platform": "woocommerce",
        "paths": [("/categoria-producto/one-piece-card-game/", False)],
    },
    "https://thirdimpact.cl": {
        "platform": "bsale",
        "paths": [("/collection/one-piece-card-game", False)],
    },
    "https://oneupstore.cl": {
        "platform": "bsale",
        "paths": [("/collection/one-piece", False)],
    },
    # Royal: se usa la versión /cl/ del sitio (la /es/ le llegaba vacía al bot).
    # La portada (True = solo títulos One Piece) es respaldo si la búsqueda falla.
    "https://royalcardstore.cl": {
        "platform": "prestashop",
        "paths": [
            ("/cl/busqueda?controller=search&s=One+piece", False),
            ("/cl/", True),
        ],
    },
    "https://www.wargaming.cl": {
        "platform": "bsale",
        "paths": [
            ("/collection/onepiece", False),
            ("/collection/00_pre-order_onepiece", False),
        ],
    },
    # TCG Lab (Jumpseller) no tiene categoría de sellado: se usan búsquedas de producto
    # sellado. Las cartas sueltas se descartan con los filtros de siempre.
    "https://tcglab.cl": {
        "platform": "jumpseller",
        "max_pages": 3,
        "paths": [
            ("/search/booster", False),
            ("/search/starter-deck", False),
            ("/search/double-pack", False),
            ("/search/illustration-box", False),
        ],
    },
}

# Colecciones Shopify extra que quieras forzar (URL completa). Se vigilan TODOS sus productos.
EXTRA_COLLECTIONS = [
    # "https://oasisgames.cl/collections/one-piece-sealed",
]

# Una colección/categoría cuenta como "One Piece" si su nombre/handle coincide con esto
COLLECTION_RE = re.compile(
    r"one[-\s]?piece|(^|[-\s])op([-\s]|$)|illustration[-\s]box|"
    r"premium[-\s](extra[-\s])?booster|extra[-\s]booster",
    re.IGNORECASE,
)

# Colecciones de preventa (mezclan juegos: solo se guardan productos One Piece)
PREVENTA_RE = re.compile(r"pre[-\s]?venta|pre[-\s]?order", re.IGNORECASE)

# Nombres de producto que interesan dentro de las colecciones de preventa
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
SINGLE_CARD_RE = re.compile(r"(?<![a-z])(op|eb|prb|st)-?\d{2}-\d{2,3}", re.IGNORECASE)

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
    r"^[a-z]+(-[a-z]+)?-premium-card-collection",
    re.IGNORECASE,
)

# (incluye productos de Bandai que algunas tiendas publican sin decir "One Piece",
#  p. ej. "Illustration Box Vol. 7" o "Double Pack Set Vol.12")
ONE_PIECE_RE = re.compile(
    r"one[-\s]?piece|illustration[-\s]box|double[-\s]pack[-\s]set|devil[-\s]fruits[-\s]collection|"
    r"(^|-)op-|(^|[^a-z])(op|eb|prb|st|ib|dp|ts|sd|ln|lt|df|peb)-?\d{1,2}([^0-9a-z]|$)",
    re.IGNORECASE,
)


class UnsupportedStore(Exception):
    """La tienda no es de una plataforma que el bot sepa leer (o bloquea al bot)."""


def is_unwanted(p):
    """True si el producto es carta suelta, accesorio u otro juego."""
    handle = p["handle"].lower()
    text = f"{handle} {p['title'].lower()}"
    if not ONE_PIECE_RE.search(text):
        return True
    return bool(
        UNWANTED_RE.search(text)
        or SINGLE_CARD_RE.search(text)
        or re.search(r"-\d{6}$", handle)          # cartas sueltas de La Comarca
        or re.search(r"(^|-)don(-|$)", handle)    # cartas DON!!
    )


def get_json(url, **params):
    r = fetch(url, params=params)
    r.raise_for_status()
    return r.json()


def fmt_price(price):
    return f"${price:,.0f}".replace(",", ".") if price is not None else "precio no visible"


# --- Detección de plataforma -------------------------------------------------
PLATFORM_MARKS = [
    ("tiendanube", r"mitiendanube|nuvemshop|tiendanube"),
    ("shopify", r"cdn\.shopify|myshopify"),
    ("jumpseller", r"jumpseller"),
    ("woocommerce", r"woocommerce|wp-content"),
    ("bsale", r"bsale"),
    ("magento", r"magento"),          # antes "mage-" coincidía con "image-" (falso positivo)
    ("prestashop", r"prestashop"),
    ("wix", r"wixstatic"),
    ("vtex", r"vtexassets|vtex\.com"),
]
WOO_APIS = ("/wp-json/wc/store/v1", "/wp-json/wc/store")


def _probe_json(url, key=None, **params):
    """Devuelve el JSON si la URL responde JSON válido (y trae `key`), si no None."""
    try:
        r = fetch(url, params=params, timeout=20, tries=2)
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    if key is not None and not (isinstance(data, dict) and key in data):
        return None
    return data


def detect_platform(base):
    """Devuelve (plataforma, detalle). plataforma: shopify | woocommerce | html | otra."""
    if base in HTML_STORES:
        return "html", ""
    if _probe_json(f"{base}/products.json", key="products", limit=1) is not None:
        return "shopify", ""
    for api in WOO_APIS:
        if isinstance(_probe_json(f"{base}{api}/products", per_page=1), list):
            return "woocommerce", api
    try:
        r = fetch(base, timeout=20)
    except requests.RequestException as e:
        return "otra", f"sin conexión ({type(e).__name__})"
    if r.status_code in (401, 403, 429, 503):
        return "otra", f"bloquea al bot (HTTP {r.status_code})"
    marks = [n for n, pat in PLATFORM_MARKS if re.search(pat, r.text, re.IGNORECASE)]
    for name in marks:
        if name in HTML_PRESETS:       # plataforma que sabemos leer desde el HTML: se autodetectan sus categorías
            return f"auto:{name}", ""
    return "otra", f"plataforma={marks or '?'} (HTTP {r.status_code})"


# --- Shopify -------------------------------------------------------------------
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


def scan_shopify(base):
    targets = [(f"{base}/collections/{h}", only_title, h) for h, only_title in discover_collections(base)]
    targets += [(u.rstrip("/"), False, u) for u in EXTRA_COLLECTIONS if u.startswith(base)]
    found = {}
    for col_url, only_title, handle in targets:
        for p in fetch_products(col_url):
            if is_unwanted(p):
                continue
            if only_title and not TITLE_RE.search(p["title"]):
                continue
            prices = [float(v["price"]) for v in p["variants"] if v.get("available")]
            found[f"{base}/products/{p['handle']}"] = {
                "title": p["title"],
                "available": bool(prices),
                "price": min(prices) if prices else None,
                "image": p["images"][0]["src"] if p.get("images") else None,
                "preorder": bool(PREVENTA_RE.search(handle) or PREVENTA_RE.search(p["title"])),
            }
        time.sleep(0.5)
    print(f"{base}: Shopify, {len(targets)} colecciones, {len(found)} productos")
    return found


# --- WooCommerce (Store API pública, sin credenciales) --------------------------
def _woo_paged(url, **params):
    items, page = [], 1
    while True:
        batch = get_json(url, per_page=100, page=page, **params)
        items += batch
        if len(batch) < 100:
            return items
        page += 1
        time.sleep(0.5)


def scan_woocommerce(base, api):
    cats = _woo_paged(f"{base}{api}/products/categories")
    targets = []   # (params, solo_titulos, nombre_categoria)
    for c in cats:
        text = f"{c.get('slug', '')} {html.unescape(c.get('name', ''))}"
        if c.get("count", 1) == 0:
            continue
        if not INCLUDE_SINGLES and SINGLES_RE.search(text):
            continue
        if COLLECTION_RE.search(text):
            targets.append(({"category": c["id"]}, False, text))
        elif PREVENTA_RE.search(text):
            targets.append(({"category": c["id"]}, True, text))
    if not targets:   # sin categorías reconocibles: búsqueda por texto
        targets = [({"search": "one piece"}, False, "")]

    found = {}
    for params, only_title, cat_text in targets:
        for p in _woo_paged(f"{base}{api}/products", **params):
            title = html.unescape(p.get("name", ""))
            if is_unwanted({"handle": p.get("slug", ""), "title": title}):
                continue
            if only_title and not TITLE_RE.search(title):
                continue
            pr = p.get("prices") or {}
            try:
                price = int(pr["price"]) / 10 ** int(pr.get("currency_minor_unit", 0))
            except (KeyError, TypeError, ValueError):
                price = None
            # se arma el link con el dominio configurado (evita duplicados www / sin www)
            path = urlparse(p.get("permalink", "")).path or f"/?p={p.get('id')}"
            cat_names = " ".join(html.unescape(c.get("name", "")) for c in p.get("categories", []))
            found[base + path] = {
                "title": title,
                "available": bool(p.get("is_in_stock")) and p.get("is_purchasable", True),
                "price": price,
                "image": (p.get("images") or [{}])[0].get("src"),
                "preorder": bool(PREVENTA_RE.search(f"{cat_text} {cat_names} {title}")),
            }
        time.sleep(0.5)
    print(f"{base}: WooCommerce, {len(targets)} categorías, {len(found)} productos")
    return found


# --- Tiendas leídas desde HTML (Tiendanube, Bsale, Jumpseller, WooCommerce, PrestaShop) ---
OUT_OF_STOCK_RE = re.compile(
    r"\bagotado\b|sin stock|fuera de stock|out[-\s]of[-\s]stock|no disponible", re.IGNORECASE)
OUT_OF_STOCK_CSS = '[class*="outofstock"], [class*="out-of-stock"], [class*="out_of_stock"]'
# "$19.990" / "$ 6990" / "19.990 $" (el formato con $ al final exige separador de miles,
# para no confundir "Vol. 7 $35.990" con un precio de $7)
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:\.\d{3})+|\d+)|(?<![\d.])(\d{1,3}(?:\.\d{3})+)\s?\$")


def _host(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _price_clp(text):
    vals = [int((a or b).replace(".", "")) for a, b in PRICE_RE.findall(text)]
    vals = [v for v in vals if v >= 100]          # descarta $0 del carrito y números sueltos
    return min(vals) if vals else None


def _img_url(card, page_url):
    img = card.find("img")
    if not img:
        return None
    for attr in ("data-srcset", "srcset", "data-src", "data-lazy-src", "src"):
        val = img.get(attr)
        if not val:
            continue
        url = val.split(",")[0].strip().split(" ")[0]
        if url.startswith("data:"):
            continue
        return urljoin(page_url, url)
    return None


def _make_matcher(page_url, product_re, skip_paths):
    """Devuelve una función: <a> -> (id_producto, ruta) o None."""
    host = _host(page_url)

    def match(a):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return None
        u = urlparse(urljoin(page_url, href))
        if _host(u.geturl()) != host or u.path.rstrip("/") in skip_paths:
            return None
        m = product_re.search(u.path)
        return (m.group(1), u.path) if m else None
    return match


def _card_title(card, anchors):
    title = next((a["title"].strip() for a in anchors if a.get("title", "").strip()), "")
    if not title:
        h = card.find(["h2", "h3", "h4", "h5"])
        title = h.get_text(" ", strip=True) if h else ""
    if not title:
        img = card.find("img", alt=True)
        title = img["alt"].strip() if img else ""
    if not title:
        title = max((a.get_text(" ", strip=True) for a in anchors), key=len, default="")
    # algunas tiendas (PrestaShop) recortan el título: "One Piece Card Game: The..."
    if title.endswith(("...", "…")):
        img = card.find("img", alt=True)
        alt = img["alt"].strip() if img else ""
        if len(alt) > len(title.rstrip(".… ")):
            title = alt
    return title


def parse_html_listing(html_text, page_url, product_re, skip_paths, require_price=False):
    """Devuelve {id: {title, path, available, price, image}} de una página de categoría."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError("falta beautifulsoup4 (pip install beautifulsoup4)") from e
    soup = BeautifulSoup(html_text, "html.parser")
    match = _make_matcher(page_url, product_re, skip_paths)
    items = {}
    for a in soup.find_all("a", href=True):
        hit = match(a)
        if not hit or hit[0] in items:
            continue
        pid, path = hit
        # la "tarjeta" es el elemento más grande que contiene solo este producto
        card, levels = a, 0
        while card.parent is not None and card.parent.name not in ("body", "html", "[document]") and levels < 8:
            ids = {h[0] for h in map(match, card.parent.find_all("a", href=True)) if h}
            if len(ids) > 1:
                break
            card, levels = card.parent, levels + 1
        text = card.get_text(" ", strip=True)
        price = _price_clp(text)
        if require_price and price is None:
            continue                     # link de menú/categoría, no un producto
        anchors = [x for x in card.find_all("a", href=True) if (match(x) or (None,))[0] == pid]
        classes = " ".join(card.get("class") or [])
        out = bool(OUT_OF_STOCK_RE.search(text) or re.search(r"out.?of.?stock", classes)
                   or card.select_one(OUT_OF_STOCK_CSS))
        items[pid] = {
            "title": _card_title(card, anchors) or pid,
            "path": path,
            "available": not out,
            "price": price,
            "image": _img_url(card, page_url),
        }
    return items


def _debug_links(base, r, limit=12):
    """Si una categoría no entrega productos, muestra qué links internos tiene (para ajustar)."""
    hrefs = re.findall(r'href=["\']([^"\'#]+)', r.text)
    internal = []
    for h in hrefs:
        u = urljoin(r.url, h)
        if _host(u) == _host(r.url) and u not in internal:
            internal.append(u)
    m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:60] if m else "-"
    print(f"DEBUG {base}: HTTP {r.status_code} url={r.url} | {len(r.text)} bytes, título='{title}' | "
          f"{len(internal)} links internos, ejemplos: {internal[:limit]}")


def scan_html_store(base, cfg):
    preset = {**HTML_PRESETS[cfg.get("platform", "tiendanube")], **cfg}
    product_re = re.compile(preset["product_re"], re.IGNORECASE)
    skip_paths = {""} | {urlparse(p).path.rstrip("/") for p, _ in cfg["paths"]}
    found = {}
    for path, only_title in cfg["paths"]:
        seen = set()
        for page in range(1, preset.get("max_pages", 10) + 1):
            params = {preset["page_param"]: page} if page > 1 else None
            r = fetch(base + path, params=params)
            if r.status_code == 404 and page > 1:
                break
            r.raise_for_status()
            items = parse_html_listing(r.text, r.url, product_re, skip_paths, preset.get("require_price"))
            if page == 1 and not items:
                _debug_links(base, r)
            new = [pid for pid in items if pid not in seen]
            if not new:
                break
            seen.update(new)
            for pid in new:
                it = items.pop(pid)
                # se filtra solo por el título: el slug puede llevar sufijos aleatorios
                if is_unwanted({"handle": "", "title": it["title"]}):
                    continue
                if only_title and not TITLE_RE.search(it["title"]):
                    continue
                link_path = it.pop("path")
                if preset.get("trailing_slash"):
                    link_path = link_path.rstrip("/") + "/"
                found[base + link_path] = {
                    **it,
                    "preorder": bool(PREVENTA_RE.search(path) or PREVENTA_RE.search(it["title"])
                                     or PREVENTA_RE.search(link_path)),
                }
            time.sleep(0.5)
    print(f"{base}: HTML/{cfg.get('platform', 'tiendanube')}, {len(cfg['paths'])} categorías, "
          f"{len(found)} productos")
    return found


# --- Autodetección de categorías (tiendas HTML que no están en HTML_STORES) ----
# Categorías por rareza de carta suelta: /one-piece/l, /r, /sr, /sec, /uc, /sp...
RARITY_PATH_RE = re.compile(r"/(l|c|uc|r|sr|sec|sp|spr|p|tr|alt|leader|common|rare)/?$", re.IGNORECASE)

def auto_html_store(base, platform):
    if platform == "prestashop":
        raise UnsupportedStore("prestashop: agrega su búsqueda/categoría en HTML_STORES")
    try:
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise RuntimeError("falta beautifulsoup4 (pip install beautifulsoup4)") from e
    r = fetch(base)
    r.raise_for_status()
    product_re = re.compile(HTML_PRESETS[platform]["product_re"], re.IGNORECASE)
    host = _host(r.url)
    cands = {}
    for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        u = urlparse(urljoin(r.url, href))
        path = u.path
        if _host(u.geturl()) != host or path in ("", "/"):
            continue
        label = f"{a.get_text(' ', strip=True)} {a.get('title') or ''}".strip()
        if platform == "bsale" and not path.startswith("/collection/"):
            continue
        if platform == "woocommerce" and not re.search(r"categoria-producto|product-category", path):
            continue
        if platform == "tiendanube" and product_re.search(path):
            continue
        if platform == "jumpseller" and len(label) > 30:     # en Jumpseller productos y categorías comparten URL
            continue
        text = f"{path} {label}"
        if SINGLES_RE.search(text) or RARITY_PATH_RE.search(path):
            continue
        if COLLECTION_RE.search(text):
            cands[path] = False
        elif PREVENTA_RE.search(text):
            cands.setdefault(path, True)
    paths = sorted(cands.items(), key=lambda kv: (kv[1], len(kv[0])))[:6]   # primero las de One Piece
    if not paths:
        raise UnsupportedStore(f"{platform}: no encontré categorías de One Piece en el menú "
                               f"(agrégala a mano en HTML_STORES)")
    print(f"{base}: categorías detectadas automáticamente ({platform}): {[p for p, _ in paths]}")
    return scan_html_store(base, {"platform": platform, "paths": paths})


# --- Orquestación ------------------------------------------------------------
def scan_store(base):
    """Devuelve {link: {title, available, price, image, preorder}} de la tienda."""
    platform, detail = detect_platform(base)
    if platform == "html":
        return scan_html_store(base, HTML_STORES[base])
    if platform == "shopify":
        return scan_shopify(base)
    if platform == "woocommerce":
        return scan_woocommerce(base, detail)
    if platform.startswith("auto:"):
        return auto_html_store(base, platform.split(":", 1)[1])
    raise UnsupportedStore(detail)


def safe_scan(base):
    t0 = time.monotonic()
    try:
        return base, scan_store(base), None
    except Exception as e:  # noqa: BLE001
        return base, None, e
    finally:
        print(f"TIEMPO {base}: {time.monotonic() - t0:.1f}s")


def diagnostico(urls):
    for base in urls:
        platform, detail = detect_platform(base)
        hits = []
        try:
            page = requests.get(base, headers=HEADERS, timeout=30).text
            for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
                label = re.sub(r"<[^>]+>|\s+", " ", label).strip()
                if re.search(r"one[-\s]?piece|(^|[-/\s])op-?\d|pre-?venta", href + " " + label, re.I):
                    if href not in [h for h, _ in hits]:
                        hits.append((href, label[:50]))
        except requests.RequestException:
            pass
        print(f"DIAG {base}: {platform} {detail} | enlaces OP/preventa={hits[:10]}")


# --- Telegram (envía a TODOS los chats de TELEGRAM_CHAT_ID) -------------------
def _tg_post(method, payload):
    """POST a Telegram. Si responde 429 (límite: ~20 msg/min por grupo), espera lo que
    indica `retry_after` y reintenta una vez."""
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code == 429:
        try:
            wait = int(r.json().get("parameters", {}).get("retry_after", 5))
        except ValueError:
            wait = 5
        print(f"Telegram 429 para {payload.get('chat_id')}: espero {wait}s y reintento")
        time.sleep(min(wait, 60) + 1)
        r = requests.post(url, json=payload, timeout=30)
    return r


def _send_one(chat, text, image=None, no_preview=False):
    """Envía un mensaje a un chat. Con foto si se puede; si falla la foto, solo texto."""
    if image:
        r = _tg_post("sendPhoto", {"chat_id": chat, "photo": image, "caption": text, "parse_mode": "HTML"})
        if r.ok:
            return
        print(f"sendPhoto falló para {chat}, envío solo texto:", r.status_code, r.text)
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if no_preview:
        payload["disable_web_page_preview"] = True
    r = _tg_post("sendMessage", payload)
    if not r.ok:
        print("Telegram error:", chat, r.status_code, r.text)
    r.raise_for_status()


def telegram(text, image=None, no_preview=False):
    """Envía a cada chat de CHAT_IDS. Si uno falla (p. ej. alguien no pulsó Start),
    sigue con los demás; solo lanza error si no llegó a ninguno."""
    ok, last = False, None
    for chat in CHAT_IDS:
        try:
            _send_one(chat, text, image, no_preview)
            ok = True
        except Exception as e:  # noqa: BLE001
            print(f"No se pudo enviar al chat {chat}: {e}", file=sys.stderr)
            last = e
    if not ok and last:
        raise last


def probar_telegram():
    """Envía un mensaje de prueba a cada chat y dice cuál falla y por qué."""
    print(f"Telegram: {len(CHAT_IDS)} chat(s) configurado(s) en TELEGRAM_CHAT_ID")
    for i, chat in enumerate(CHAT_IDS, 1):
        try:
            _send_one(chat, "🔔 Prueba del bot de stock: si lees esto, este chat está bien configurado.")
            print(f"  chat #{i}: OK")
        except Exception as e:  # noqa: BLE001
            print(f"  chat #{i}: FALLÓ -> {e}")
    if len(CHAT_IDS) < 2:
        print("  (si esperabas 2 chats: el secreto TELEGRAM_CHAT_ID tiene solo 1; "
              "formato: -5415617768,7544062179)")


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
            pre = " ⏳" if p["preorder"] else ""
            lines.append(f'• <a href="{link}">{html.escape(p["title"])}</a> — {fmt_price(p["price"])}{pre}')
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
        telegram(m, no_preview=True)
        time.sleep(1)


def main():
    if os.environ.get("DIAG", "").lower() == "true":
        diagnostico(STORES)
        return
    if not TOKEN or not CHAT_IDS:
        sys.exit("Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
    if os.environ.get("TEST_TG", "").lower() == "true":
        probar_telegram()
        return
    print(f"Telegram: se enviará a {len(CHAT_IDS)} chat(s)")

    first_run = not STATE_FILE.exists()
    old = {} if first_run else json.loads(STATE_FILE.read_text())
    new, alerts, failed, skipped = {}, [], 0, 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(safe_scan, STORES))

    for base, found, err in results:
        prefix = base + "/"
        if err is not None:
            if isinstance(err, UnsupportedStore):
                print(f"SKIP {base}: {err} -> se omite (ver DIAG=true)")
                skipped += 1
            else:
                print(f"ERROR {base}: {err}", file=sys.stderr)
                failed += 1
            # se conserva el estado anterior de la tienda para no re-alertar cuando vuelva
            new.update({k: v for k, v in old.items() if k.startswith(prefix)})
            continue
        # tienda nueva (o recién soportada): se registra en silencio, sin alertas masivas
        store_first = first_run or not any(k.startswith(prefix) for k in old)
        for link, p in found.items():
            new[link] = p["available"]
            if p["available"] and not old.get(link, False) and not store_first:
                tag = "⏳ <b>Preventa</b>\n" if p["preorder"] else ""
                alerts.append((
                    f"🟢 <b>{html.escape(p['title'])}</b>\n"
                    f"{tag}"
                    + (f"Desde {fmt_price(p['price'])} CLP\n" if p["price"] is not None
                       else "Precio: ver en la tienda\n") +
                    f'<a href="{link}">Ver producto</a>',
                    p["image"],
                ))

    for msg, image in alerts:
        telegram(msg, image)
        time.sleep(1)

    ok = len(STORES) - failed - skipped
    print(f"Resumen: {ok} tiendas OK, {skipped} no soportadas, {failed} con error, {len(alerts)} alertas")

    if first_run:
        telegram(
            f"✅ Bot iniciado. Vigilando {len(new)} productos "
            f"({sum(new.values())} con stock ahora) en {ok} tiendas."
        )

    if os.environ.get("LISTAR", "").lower() == "true":
        enviar_lista(results)

    STATE_FILE.write_text(json.dumps(new, indent=1, sort_keys=True))
    sys.exit(1 if ok == 0 else 0)


if __name__ == "__main__":
    main()
