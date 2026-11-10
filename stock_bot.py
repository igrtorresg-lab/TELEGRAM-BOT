"""
Bot de stock -> Telegram.
Revisa colecciones de tiendas Shopify (products.json) y avisa cuando un producto
cuyo nombre coincide con tus palabras clave pasa a estar disponible.

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

# --- Colecciones a vigilar (agrega o quita las que quieras) ---------------
COLLECTIONS = [
    "https://www.oasisgames.cl/collections/one-piece-sealed",
    "https://www.tiendalacomarca.cl/collections/cajas-y-sobres-one-piece",
    "https://www.tiendalacomarca.cl/collections/premium-set-one-piece",
    "https://www.tiendalacomarca.cl/collections/deck-estrucuras-one-piece",
    "https://www.tiendalacomarca.cl/collections/one-piece-preventas",
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


def telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
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
            # conserva el estado anterior de esa colección
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
                alerts.append(
                    f"🟢 <b>{html.escape(title)}</b>\n"
                    f"Desde {price} CLP\n{link}"
                )

    for msg in alerts:
        telegram(msg)
        time.sleep(1)

    if first_run:
        en_stock = sum(new.values())
        telegram(
            f"✅ Bot iniciado. Vigilando {len(new)} productos "
            f"({en_stock} con stock ahora). Te aviso cuando alguno nuevo tenga stock."
        )

    STATE_FILE.write_text(json.dumps(new, indent=1, sort_keys=True))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
