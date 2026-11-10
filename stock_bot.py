"""
Bot de stock -> Telegram (autodescubre colecciones de One Piece).

Para cada tienda Shopify:
  1) lee /collections.json y se queda con las colecciones de One Piece
     (cajas, sobres, decks, premium, illustration box, preventas...)
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
    "https://bastiongoblin.com",
    "https://tecitogames.com",
    "https://tablecatgames.cl",
    "https://dyskeu.cl",
]

# Colecciones extra que quieras forzar (URL completa de la colección)
EXTRA_COLLECTIONS = [
    # "https://oasisgames.cl/collections/one-piece-sealed",
]

# Una colección cuenta como "One Piece" si su nombre/handle coincide con esto
COLLECTION_RE = re.compile(
    r"one[-\s]?piece|(^|[-\s])(op|eb|prb|st)-?\d|illustration[-\s]box|"
    r"premium[-\s](extra[-\s])?booster|extra[-\s]booster",
    re.IGNORECASE,
)
# Se excluyen cartas sueltas y accesorios: solo interesa el producto sellado
INCLUDE_SINGLES = False
SINGLES_RE = re.compile(
    r"single|suelta|accesorio|sleeve|playmat|protector|deck-?box|carpeta|album",
    re.IGNORECASE,
)


def get_json(url, **params):
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def discover_collections(base):
    cols, page = [], 1
    while True:
        batch = get_json(f"{base}/collections.json", limit=250, page=page)["collections"]
        cols += batch
        if len(batch) < 250:
            break
        page += 1
        time.sleep(0.5)
    handles = []
    for c in cols:
        text = f"{c['handle']} {c.get('title', '')}"
        if c.get("products_count", 1) == 0:
            continue
        if not COLLECTION_RE.search(text):
            continue
        if not INCLUDE_SINGLES and SINGLES_RE.search(text):
            continue
        handles.append(c["handle"])
    return handles


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
    """Devuelve {link: {title, available, price, image}} de la tienda."""
    handles = discover_collections(base)
    col_urls = [f"{base}/collections/{h}" for h in handles]
    col_urls += [u.rstrip("/") for u in EXTRA_COLLECTIONS if u.startswith(base)]
    found = {}
    for cu in col_urls:
        for p in fetch_products(cu):
            link = f"{base}/products/{p['handle']}"
            prices = [float(v["price"]) for v in p["variants"] if v.get("available")]
            found[link] = {
                "title": p["title"],
                "available": bool(prices),
                "price": min(prices) if prices else None,
                "image": p["images"][0]["src"] if p.get("images") else None,
            }
        time.sleep(0.5)
    print(f"{base}: {len(col_urls)} colecciones, {len(found)} productos")
    return found


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


def main():
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
                alerts.append((
                    f"🟢 <b>{html.escape(p['title'])}</b>\n"
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

    STATE_FILE.write_text(json.dumps(new, indent=1, sort_keys=True))
    sys.exit(1 if failed == len(STORES) else 0)


if __name__ == "__main__":
    main()
