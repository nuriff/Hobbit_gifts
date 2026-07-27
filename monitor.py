"""
Product availability monitor -> Telegram alert.

Checks a list of product URLs, and sends a Telegram message the moment
a product flips from "not available" to "available".

Run this once per invocation (cron / GitHub Actions call it repeatedly).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# --- Config -----------------------------------------------------------

# Prefer environment variables (safer for GitHub Actions secrets),
# fall back to hardcoded values for quick local testing.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

def xzone_available(soup):
    # Xzone.cz embeds proper schema.org microdata for stock status --
    # far more reliable than matching button text/wording.
    tag = soup.find(attrs={"itemprop": "availability"})
    return tag is not None and "InStock" in tag.get("href", "")


def najada_hobbit_gift_available(soup):
    # Najada's /mtg page embeds structured JSON-LD product data (name +
    # schema.org availability) directly in the HTML. Rather than match on
    # the exact string "The Hobbit Gift Bundle" (which this store doesn't
    # currently use -- they call it "Fat Pack Bundle" instead), this scans
    # every listed product name for one that contains BOTH "hobbit" and
    # "gift", catching the literal name or any future rename. If found,
    # it also checks that specific product's own stock status.
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except ValueError:
            continue
        items = data.get("itemListElement") if isinstance(data, dict) else None
        if not items:
            continue
        for entry in items:
            item = entry.get("item", {}) or {}
            name = (item.get("name") or "")
            lname = name.lower()
            if "hobbit" in lname and "gift" in lname:
                availability = (item.get("offers", {}) or {}).get("availability", "")
                if "InStock" in availability:
                    return True
    return False


def cernyrytir_available():
    # This site is a JavaScript-rendered app -- there's no usable HTML to
    # scrape directly. Instead it calls a JSON API to list tagged products.
    # Verified against the site's own minified frontend code: the buy button
    # is enabled exactly when prodStatusName == "ACTIVE" and availEshopQty > 0
    # (the "presale" flag is just a shipping-date label, it does NOT block
    # ordering in the site's own logic).
    url = "https://eshop-api.cernyrytir.eu/api/public/tagged-product/list?eshop_lang=CZ"
    payload = {
        "extendedFilter": {"tagIds": [0], "inStockOnlyEshop": False, "inStockOnlyStore": False},
        "pagination": {"page": 1, "rowsPerPage": 48, "rowsNumber": 0, "sortBy": "sortPriceSell", "descending": True},
    }
    api_headers = {**HEADERS, "Content-Type": "application/json", "Origin": "https://cernyrytir.cz",
                   "Referer": "https://cernyrytir.cz/"}
    resp = requests.post(url, headers=api_headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    for item in data.get("list", []):
        name = item.get("sortName", "")
        lname = name.lower()
        if "hobbit" in lname and "gift" in lname:
            merch = item.get("merch", {}) or {}
            return merch.get("prodStatusName") == "ACTIVE" and (merch.get("availEshopQty") or 0) > 0
    return False


# One entry per page being checked.
# `available_if` is the rule that decides "can I buy this right now?".
# Each was verified against the site's real HTML -- see comments per entry.
CHECKS = [
    {
        "name": "Planeta Her - MTG The Hobbit Gift Bundle",
        "url": "https://www.planetaher.cz/magic--the-gathering-the-hobbit-gift-bundle/",
        # This site (Shoptet platform) removes the buy button entirely when a
        # product can't be ordered yet, and shows "Vyprodáno" instead. So the
        # buy button's presence is the signal, not any particular wording.
        "available_if": lambda soup: (
            soup.find(attrs={"data-testid": "buttonAddToCart"}) is not None
        ),
    },
    {
        "name": "Xzone - MTG The Hobbit Gift Bundle",
        "url": "https://www.xzone.cz/karetni-hra-magic-the-gathering-the-hobbit-gift-bundle",
        "available_if": xzone_available,
    },
    {
        "name": "Vesely Drak - MTG The Hobbit Gift Bundle",
        "url": "https://www.vesely-drak.cz/produkty/fat-pack/18832-magic-the-gathering-the-hobbit-gift-bundle/",
        # This site reuses the same CSS class ("buy-button") whether or not
        # the product is purchasable -- but only swaps in a real <button> tag
        # when it can actually be ordered. When unavailable it's an <a> tag
        # linking to alternatives instead. So we check for the tag type, not the class.
        "available_if": lambda soup: (
            soup.find("button", attrs={"name": "vlozit_do_kosiku"}) is not None
        ),
    },
    {
        "name": "Najada - The Hobbit Gift Bundle (listing page)",
        "url": "https://www.najada.games/mtg",
        "available_if": najada_hobbit_gift_available,
    },
    {
        "name": "Cerny Rytir - The Hobbit Bundle",
        "url": "https://cernyrytir.cz/tagged/0?iso_e=false&iso_s=false&sort_by=&rpp=48",
        "custom_check": cernyrytir_available,
    },
]


# --- Core logic ---------------------------------------------------------

def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_all() -> None:
    state = load_state()

    for check in CHECKS:
        name, url = check["name"], check["url"]
        try:
            if "custom_check" in check:
                # Some sites (e.g. JS-rendered apps) need their own request
                # logic entirely, rather than a plain GET + HTML scrape.
                available = bool(check["custom_check"]())
            else:
                resp = requests.get(url, headers=HEADERS, timeout=20)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")
                available = bool(check["available_if"](soup))
        except Exception as e:
            print(f"[error] {name}: {e}")
            continue

        was_available = state.get(name, False)
        print(f"[check] {name}: available={available} (was {was_available})")

        if available and not was_available:
            send_telegram(f"\U0001F7E2 IN STOCK: {name}\n{url}")
            state["_last_alert_at"] = datetime.now(timezone.utc).isoformat()

        state[name] = available

    save_state(state)


def daily_summary() -> None:
    """Send a once-a-day reassurance ping if nothing has been found lately."""
    state = load_state()
    last_alert_raw = state.get("_last_alert_at")

    recently_alerted = False
    if last_alert_raw:
        try:
            last_alert_at = datetime.fromisoformat(last_alert_raw)
            recently_alerted = (datetime.now(timezone.utc) - last_alert_at) < timedelta(hours=24)
        except ValueError:
            pass

    if recently_alerted:
        print("[daily] Skipping daily summary -- an alert already went out within the last 24h.")
        return

    site_names = "\n".join(f"- {c['name']}" for c in CHECKS)
    send_telegram(
        "\U0001F4CB Daily check-in: no product found in stock in the past 24 hours.\n"
        f"Still watching:\n{site_names}"
    )
    print("[daily] Sent daily summary.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "daily":
        daily_summary()
    else:
        check_all()
