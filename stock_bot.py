"""
Bot de stock -> Telegram.
Revisa tiendas Shopify (products.json) y avisa, con imagen y link, cuando un
producto cuyo nombre coincide con tus palabras clave pasa a estar disponible.

Variables de entorno requeridas: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = Path("state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# --- Tiendas a vigilar (una por línea, SIEMPRE con coma al final) ---------
COLLECTIONS = [
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
    # Tiendaoops: agregar cuando sepamos su estructura
]

# --- Palabras clave (regex, sin distinguir mayúsculas) -------------------
PATTERNS = [
    r"one piece",
    r"\bop-",              # "OP-14"
    r"\bop-?\d{2}\b",      # "OP14" sin guion
    r"illustration box vol",
    r"premium (extra )?booster",
]
REGEX = re.compile("|".join(PATTERNS), re.IGNORECASE)


def fetch_collection(url):
    url = url.rstrip("/")
    products, page = [], 1
    while True:
        r = requests.get(
            f"{url}/products.json",
            params={"limit": 250, "page": page},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()["products"]
        products += batch
        if len(batch) < 250:
            break
        page += 1
        time.sleep(1)
    return products


def base_of(url):
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1)


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
    new = {}
    alerts = []
    failed = 0

    for col in COLLECTIONS:
        try:
            products = fetch_collection(col)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {col}: {e}", file=sys.stderr)
            failed += 1
            # conserva el estado anterior de esa tienda
            prefix = base_of(col) + "/products/"
            new.update({k: v for k, v in old.items() if k.startswith(prefix)})
            continue

        for p in products:
            title = p["title"]
            if not REGEX.search(title):
                continue
            link = f"{base_of(col)}/products/{p['handle']}"
            avail_prices = [float(v["price"]) for v in p["variants"] if v.get("available")]
            available = bool(avail_prices)
            new[link] = available
            if available and not old.get(link, False) and not first_run:
                price = f"${min(avail_prices):,.0f}".replace(",", ".")
                image = p["images"][0]["src"] if p.get("images") else None
                alerts.append((
                    f"🟢 <b>{html.escape(title)}</b>\n"
                    f"Desde {price} CLP\n"
                    f'<a href="{link}">Ver producto</a>',
                    image,
                ))

    for msg, image in alerts:
        telegram(msg, image)
        time.sleep(1)

    if first_run:
        en_stock = sum(new.values())
        telegram(
            f"✅ Bot iniciado. Vigilando {len(new)} productos "
            f"({en_stock} con stock ahora). Te aviso cuando alguno nuevo tenga stock."
        )

    STATE_FILE.write_text(json.dumps(new, indent=1, sort_keys=True))
    # Solo marca error si fallaron TODAS las tiendas
    sys.exit(1 if failed == len(COLLECTIONS) else 0)


if __name__ == "__main__":
    main()
