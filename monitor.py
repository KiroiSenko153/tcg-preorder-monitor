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


def is_blocked_name(text: str) -> bool:
    low = normalize_key(text)
    blocked = [
        "datenschutz",
        "privacy",
        "impressum",
        "imprint",
        "agb",
        "widerruf",
        "cancellation",
        "shipping",
        "payment",
        "kontakt",
        "contact",
        "wishlist",
        "basket",
        "register",
        "login",
        "log in",
        "terms",
        "cookies",
        "manufacturer",
        "manufacturers",
        "sort order",
        "filters",
        "language",
        "price range",
        "items found",
    ]
    return any(x in low for x in blocked)


def looks_like_product_href(href: str) -> bool:
    low = (href or "").lower()
    blocked = [
        "/privacy",
        "/datenschutz",
        "/impressum",
        "/agb",
        "/widerruf",
        "/kontakt",
        "/contact",
        "/login",
        "/register",
    ]
    if any(x in low for x in blocked):
        return False

    # molti prodotti hanno URL dedicato che non è una category page
    return True


def parse_products_from_jsonld(soup: BeautifulSoup, category: str) -> list[dict]:
    products = []
    seen = set()

    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        nodes = data if isinstance(data, list) else [data]

        for node in nodes:
            if not isinstance(node, dict):
                continue

            # caso ItemList
            if node.get("@type") == "ItemList":
                for item in node.get("itemListElement", []):
                    if not isinstance(item, dict):
                        continue
                    target = item.get("item") if isinstance(item.get("item"), dict) else item
                    name = normalize_text(target.get("name", ""))
                    url = absolute_url(target.get("url", ""))
                    if not name or not url or is_blocked_name(name):
                        continue

                    pid = (category, normalize_key(name), url)
                    if pid in seen:
                        continue
                    seen.add(pid)

                    products.append({
                        "category": category,
                        "name": name,
                        "url": url,
                        "status": "UNKNOWN",
                        "price": "",
                        "available_from": "",
                    })

            # caso Product
            if node.get("@type") == "Product":
                name = normalize_text(node.get("name", ""))
                url = absolute_url(node.get("url", ""))
                if not name or not url or is_blocked_name(name):
                    continue

                price = ""
                offers = node.get("offers")
                if isinstance(offers, dict):
                    maybe_price = offers.get("price")
                    if maybe_price:
                        price = str(maybe_price)

                pid = (category, normalize_key(name), url)
                if pid in seen:
                    continue
                seen.add(pid)

                products.append({
                    "category": category,
                    "name": name,
                    "url": url,
                    "status": "UNKNOWN",
                    "price": price,
                    "available_from": "",
                })

    return products


def parse_products_from_links(soup: BeautifulSoup, category: str) -> list[dict]:
    products = []
    seen = set()

    # prima prova a cercare box prodotto più tipiche
    candidate_containers = soup.select(
        ".productbox, .product-box, .product--box, .card, .product"
    )

    for container in candidate_containers:
        a = container.find("a", href=True)
        if not a:
            continue

        name = normalize_text(a.get_text(" ", strip=True))
        href = absolute_url(a.get("href", ""))

        full_text = normalize_text(container.get_text(" ", strip=True))

        if not name or is_blocked_name(name) or not looks_like_product_href(href):
            continue

        # richiedi almeno un segnale da prodotto
        if not (
            extract_price(full_text)
            or detect_status(full_text) != "UNKNOWN"
            or "booster" in normalize_key(name)
            or "display" in normalize_key(name)
            or "deck" in normalize_key(name)
            or "riftbound" in normalize_key(name)
        ):
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


def parse_products(html_text: str, category: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")

    # 1) prova JSON-LD
    jsonld_products = parse_products_from_jsonld(soup, category)
    if jsonld_products:
        log(f"{category}: trovati {len(jsonld_products)} prodotti da JSON-LD")
        return jsonld_products

    # 2) fallback su box/link
    link_products = parse_products_from_links(soup, category)
    if link_products:
        log(f"{category}: trovati {len(link_products)} prodotti da box/link")
        return link_products

    return []


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
    old_state = load_state()
    all_products = []

    for category, url in PAGES.items():
        try:
            log(f"Controllo {category}: {url}")
            html_text = fetch_html(url)
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
