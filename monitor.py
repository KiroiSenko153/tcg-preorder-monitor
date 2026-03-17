import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


PAGES = {
    "riftbound": "https://games-island.eu/en/c/Card-Games/Riftbound-League-of-Legends",
    "onepiece": "https://games-island.eu/en/c/Card-Games/One-Piece-Booster-Display__English",
    "magic": "https://games-island.eu/en/c/Magic-The-Gathering/MtG-Booster-Boxes-English",
}

STATE_FILE = "state.json"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


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
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return normalize_text(m.group(1))
    return ""


def extract_available_from(text: str) -> str:
    m = re.search(r"Available from:\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})", text, re.IGNORECASE)
    return m.group(1) if m else ""


def is_blocked_name(text: str) -> bool:
    low = normalize_key(text)
    blocked = [
        "datenschutz", "privacy", "impressum", "imprint", "agb", "widerruf",
        "cancellation", "shipping", "payment", "kontakt", "contact",
        "wishlist", "basket", "register", "login", "log in", "terms",
        "cookies", "manufacturer", "manufacturers", "sort order",
        "filters", "language", "price range", "items found", "please wait",
        "validating", "blacklisted",
    ]
    return any(x in low for x in blocked)


def looks_like_product(name: str, href: str, full_text: str) -> bool:
    if not name or is_blocked_name(name):
        return False

    href_low = (href or "").lower()
    if any(x in href_low for x in ["/privacy", "/datenschutz", "/impressum", "/agb", "/widerruf"]):
        return False

    signals = [
        "booster", "display", "deck", "box", "riftbound",
        "one piece", "magic", "mtg", "league of legends",
    ]
    hay = f"{normalize_key(name)} {normalize_key(full_text)}"
    return any(s in hay for s in signals)


def fetch_rendered_html(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 2200},
        )
        page = context.new_page()

        page.goto(url, wait_until="domcontentloaded", timeout=90000)

        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(5000)

        html = page.content()
        browser.close()
        return html


def parse_products(html_text: str, category: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    products = []
    seen = set()

    containers = soup.select("a[href]")
    for a in containers:
        href = absolute_url(a.get("href", ""))
        name = normalize_text(a.get_text(" ", strip=True))

        if not href or not name:
            continue

        parent = a
        for _ in range(5):
            if parent.parent is None:
                break
            parent = parent.parent

        full_text = normalize_text(parent.get_text(" ", strip=True))

        if not looks_like_product(name, href, full_text):
            continue

        pid = (category, normalize_key(name), href)
        if pid in seen:
            continue
        seen.add(pid)

        products.append({
            "category": category,
            "name": name,
            "url": href,
            "status": detect_status(full_text),
            "price": extract_price(full_text),
            "available_from": extract_available_from(full_text),
        })

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
    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()


def make_product_id(product: dict) -> str:
    return f"{product['category']}|{normalize_key(product['name'])}"


def compare_states(old_state: dict, new_products: list[dict]):
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
                "\n".join([
                    "🚨 GAMES ISLAND - NUOVO PRODOTTO",
                    f"Categoria: {cur['category'].upper()}",
                    f"Nome: {cur['name']}",
                    f"Stato: {cur['status']}",
                    f"Prezzo: {cur['price'] or 'N/D'}",
                    f"Data: {cur['available_from'] or 'N/D'}",
                    cur["url"],
                ])
            )
            new_state[pid] = cur
            continue

        prev_status = prev.get("status", "UNKNOWN")
        cur_status = cur.get("status", "UNKNOWN")

        if prev_status != cur_status:
            alerts.append(
                "\n".join([
                    "🚨 GAMES ISLAND - CAMBIO STATO",
                    f"Categoria: {cur['category'].upper()}",
                    f"Nome: {cur['name']}",
                    f"Stato: {prev_status} -> {cur_status}",
                    f"Prezzo: {cur['price'] or 'N/D'}",
                    f"Data: {cur['available_from'] or 'N/D'}",
                    cur["url"],
                ])
            )

        new_state[pid] = cur

    return new_state, alerts


def run() -> int:
    old_state = load_state()
    all_products = []

    for category, url in PAGES.items():
        try:
            log(f"Controllo {category}: {url}")
            html_text = fetch_rendered_html(url)

            if "blacklisted" in html_text.lower() or "validating" in html_text.lower():
                log(f"{category}: pagina bloccata o challenge rilevata")
                continue

            products = parse_products(html_text, category)
            log(f"{category}: trovati {len(products)} prodotti finali")
            all_products.extend(products)
        except Exception as exc:
            log(f"Errore su {category}: {exc}")

    if not all_products:
        log("Nessun prodotto trovato. Mantengo lo stato attuale e termino senza errore.")
        return 0

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


if __name__ == "__main__":
    sys.exit(run())
