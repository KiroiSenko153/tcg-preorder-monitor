import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup


PAGES = {
    "riftbound": "https://games-island.eu/en/c/Card-Games/Riftbound-League-of-Legends",
    "onepiece": "https://games-island.eu/en/c/Card-Games/One-Piece-Booster-Display__English",
    "magic": "https://games-island.eu/en/c/Magic-The-Gathering/MtG-Booster-Boxes-English",
}

STATE_FILE = "state.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,it-IT;q=0.8,it;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}")


def normalize_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_key(text: str) -> str:
    return normalize_text(text).lower()


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def absolute_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return "https://games-island.eu" + href


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=40)
    response.raise_for_status()
    return response.text


def detect_status(text: str) -> str:
    t = normalize_key(text)

    if "available immediately" in t or "in stock" in t:
        return "IN STOCK"
    if "currently out of stock" in t or "out of stock" in t:
        return "OUT OF STOCK"
    if "pre-order" in t or "pre order" in t or "preorders possible" in t:
        return "PRE-ORDER"
    if "available from:" in t or "available from" in t:
        return "COMING SOON"

    return "UNKNOWN"


def extract_price(text: str) -> str:
    patterns = [
        r"(\d{1,4},\d{2}\s*€)",
        r"(\d{1,4}\.\d{2}\s*€)",
        r"(EUR\s*\d{1,4}[.,]\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return normalize_text(match.group(1))
    return ""


def extract_available_from(text: str) -> str:
    match = re.search(
        r"Available from:\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def looks_like_product_name(text: str) -> bool:
    t = normalize_text(text)
    low = t.lower()

    if len(t) < 10:
        return False

    blocked = [
        "filters and sort order",
        "sort order",
        "language",
        "manufacturers",
        "price range",
        "basket",
        "log in",
        "register",
        "wishlist",
        "privacy",
        "terms",
        "imprint",
        "cookies",
        "items found",
    ]
    if any(b in low for b in blocked):
        return False

    return True


def parse_products(html_text: str, category: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    products = []
    seen = set()

    for a in soup.find_all("a", href=True):
        name = normalize_text(a.get_text(" ", strip=True))
        if not looks_like_product_name(name):
            continue

        href = absolute_url(a.get("href", ""))
        if not href:
            continue

        container = a
        for _ in range(5):
            parent = container.parent
            if parent is None:
                break
            container = parent
            if len(normalize_text(container.get_text(" ", strip=True))) > 60:
                break

        full_text = normalize_text(container.get_text(" ", strip=True))
        status = detect_status(full_text)
        price = extract_price(full_text)
        available_from = extract_available_from(full_text)

        key = (category, normalize_key(name), href)
        if key in seen:
            continue
        seen.add(key)

        products.append(
            {
                "category": category,
                "name": name,
                "url": href,
                "status": status,
                "price": price,
                "available_from": available_from,
            }
        )

    return products


def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram non configurato: salto invio messaggio.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()


def make_product_id(product: dict) -> str:
    return f"{product['category']}|{normalize_key(product['name'])}"


def compare_states(old_state: dict, new_products: list[dict]) -> tuple[dict, list[str]]:
    new_state = old_state.copy()
    alerts = []

    current = {}
    for product in new_products:
        pid = make_product_id(product)
        current[pid] = {
            "category": product["category"],
            "name": product["name"],
            "url": product["url"],
            "status": product["status"],
            "price": product["price"],
            "available_from": product["available_from"],
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }

    for pid, cur in current.items():
        prev = old_state.get(pid)

        if prev is None:
            alerts.append(
                "\n".join(
                    [
                        "🚨 GAMES ISLAND - NUOVO PRODOTTO",
                        f"Categoria: {cur['category'].upper()}",
                        f"Nome: {cur['name']}",
                        f"Stato: {cur['status']}",
                        f"Prezzo: {cur['price'] or 'N/D'}",
                        f"Data: {cur['available_from'] or 'N/D'}",
                        cur["url"],
                    ]
                )
            )
            new_state[pid] = cur
            continue

        prev_status = prev.get("status", "UNKNOWN")
        cur_status = cur.get("status", "UNKNOWN")

        if prev_status != cur_status:
            alerts.append(
                "\n".join(
                    [
                        "🚨 GAMES ISLAND - CAMBIO STATO",
                        f"Categoria: {cur['category'].upper()}",
                        f"Nome: {cur['name']}",
                        f"Stato: {prev_status} -> {cur_status}",
                        f"Prezzo: {cur['price'] or 'N/D'}",
                        f"Data: {cur['available_from'] or 'N/D'}",
                        cur["url"],
                    ]
                )
            )

        new_state[pid] = cur

    return new_state, alerts


def run() -> int:
    try:
        old_state = load_state()
        all_products = []

        for category, url in PAGES.items():
            log(f"Controllo {category}: {url}")
            html_text = fetch_html(url)
            products = parse_products(html_text, category)
            log(f"{category}: trovati {len(products)} prodotti")
            all_products.extend(products)

        if not all_products:
            log("Nessun prodotto trovato.")
            return 1

        if not old_state:
            log("Primo avvio: inizializzazione silenziosa di state.json")
            initial_state = {}
            for product in all_products:
                pid = make_product_id(product)
                initial_state[pid] = {
                    "category": product["category"],
                    "name": product["name"],
                    "url": product["url"],
                    "status": product["status"],
                    "price": product["price"],
                    "available_from": product["available_from"],
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                }
            save_state(initial_state)
            return 0

        new_state, alerts = compare_states(old_state, all_products)
        save_state(new_state)

        if not alerts:
            log("Nessuna variazione rilevata.")
            return 0

        for alert in alerts:
            send_telegram_message(alert)

        return 0

    except Exception as exc:
        log(f"Errore: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run())
