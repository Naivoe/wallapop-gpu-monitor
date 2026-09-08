"""
Wallapop + eBay + Milanuncios GPU Flip Monitor - GitHub Actions-versie
------------------------------------------------------------------------
Draait EEN keer per aanroep (bedoeld om via een geplande GitHub Actions
workflow elke paar minuten opgestart te worden). Controleert meerdere
tweedehands-bronnen op de GPU's uit config.json, houdt marktprijzen bij
om automatisch een redelijke inkoop-/verkoopprijs te bepalen, en stuurt
nieuwe interessante advertenties naar een Discord-webhook.

Bronnen:
- Wallapop: niet-officiele interne zoek-API (kan zonder aankondiging
  wijzigen, zie README.md).
- eBay: officiele, gratis Browse API (vereist een gratis developer-
  account, zie README.md voor het aanmaken van EBAY_APP_ID/EBAY_CERT_ID).
- Milanuncios: leest de JSON-data die de website zelf al insluit in de
  pagina (__NEXT_DATA__). Dit is EXPERIMENTEEL — de exacte structuur kon
  niet vooraf getest worden. Bij problemen print de code een debug-regel
  met de beschikbare velden; zie README.md voor hoe je dat gebruikt om
  de paden in extract_milanuncios_fields() te herstellen.

Vinted en Facebook Marketplace zitten hier bewust NIET in: beide
beveiligen zich met zware anti-bot-systemen (DataDome resp. rotatende
GraphQL-tokens + browser-fingerprinting + blokkades op datacenter-IP's).
Dat is met gratis middelen niet betrouwbaar te automatiseren voor een
onbemande cloud-taak. Zie README.md voor de alternatieven (hun eigen
ingebouwde zoek-alerts).
"""

import html
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"
SEEN_ADS_PATH = Path(__file__).parent / "seen_ads.json"
HITS_LOG_PATH = Path(__file__).parent / "hits_log.json"
DOCS_DIR = Path(__file__).parent / "docs"
DASHBOARD_PATH = DOCS_DIR / "index.html"

WALLAPOP_SEARCH_URL = "https://api.wallapop.com/api/v3/search"
EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_MARKETPLACE = "EBAY_ES"
MILANUNCIOS_SEARCH_URL = "https://www.milanuncios.com/anuncios/"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
EBAY_APP_ID = os.environ.get("EBAY_APP_ID")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID")

WALLAPOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "X-DeviceOS": "0",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Configuratie en lokale opslag
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Bron 1: Wallapop
# ---------------------------------------------------------------------------

def search_wallapop(search_term):
    """Vraagt Wallapop's (niet-officiele) zoek-API aan.
    Geeft (genormaliseerde items, foutmelding) terug."""
    params = {
        "source": "search_box",
        "keywords": search_term,
        "latitude": 40.4168,
        "longitude": -3.7038,
        "order_by": "newest",
    }
    try:
        response = requests.get(
            WALLAPOP_SEARCH_URL, headers=WALLAPOP_HEADERS, params=params, timeout=15
        )
        if response.status_code == 400:
            error = f"Wallapop 400 Bad Request voor '{search_term}': {response.text[:300]}"
            print(f"[FOUT] {error}")
            return [], error
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        error = f"Kon Wallapop niet bereiken voor '{search_term}': {e}"
        print(f"[FOUT] {error}")
        return [], error
    except ValueError:
        error = f"Wallapop: geen geldige JSON voor '{search_term}'."
        print(f"[FOUT] {error}")
        return [], error

    raw_items = None
    try:
        raw_items = data["data"]["section"]["payload"]["items"]
    except (KeyError, TypeError):
        pass
    if raw_items is None:
        try:
            raw_items = data["search_objects"]
        except (KeyError, TypeError):
            pass
    if raw_items is None:
        error = (
            f"Onbekende Wallapop JSON-structuur voor '{search_term}'. "
            "Wallapop heeft mogelijk hun API-formaat aangepast."
        )
        print(f"[WAARSCHUWING] {error}")
        return [], error

    items = [extract_wallapop_fields(raw) for raw in raw_items]
    items = [i for i in items if i["id"] and i["title"] and i["price"] is not None]
    return items, None


def extract_wallapop_fields(item):
    item_id = item.get("id") or item.get("item_id")
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    title = item.get("title") or content.get("title") or ""

    price = None
    if "price" in item:
        p = item["price"]
        price = p.get("amount") if isinstance(p, dict) else p
    elif "price" in content:
        p = content["price"]
        price = p.get("amount") if isinstance(p, dict) else p

    web_slug = item.get("web_slug") or content.get("web_slug")
    link = f"https://es.wallapop.com/item/{web_slug}" if web_slug else None

    created = (
        item.get("creation_date")
        or item.get("modified_date")
        or content.get("creation_date")
    )
    posted_at = None
    if created:
        try:
            created_int = int(created)
            ts = created_int / 1000 if created_int > 10_000_000_000 else created_int
            posted_at = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, OSError):
            posted_at = None

    return {
        "id": f"wallapop:{item_id}" if item_id else None,
        "title": title,
        "price": float(price) if price is not None else None,
        "link": link,
        "posted_at": posted_at,
        "source": "Wallapop",
    }


# ---------------------------------------------------------------------------
# Bron 2: eBay (officiele, gratis Browse API)
# ---------------------------------------------------------------------------

def get_ebay_token():
    """Haalt een tijdelijk (application access) token op via de gratis
    client-credentials-flow. Geeft (token, foutmelding) terug."""
    if not EBAY_APP_ID or not EBAY_CERT_ID:
        return None, "EBAY_APP_ID / EBAY_CERT_ID niet ingesteld — eBay wordt overgeslagen."
    try:
        response = requests.post(
            EBAY_TOKEN_URL,
            auth=(EBAY_APP_ID, EBAY_CERT_ID),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json()["access_token"], None
    except requests.RequestException as e:
        return None, f"Kon geen eBay-token ophalen: {e}"
    except (KeyError, ValueError):
        return None, "eBay gaf een onverwacht antwoord bij het ophalen van een token."


def search_ebay(search_term, token):
    """Vraagt eBay's officiele Browse API aan.
    Geeft (genormaliseerde items, foutmelding) terug."""
    if not token:
        return [], None  # al gemeld via get_ebay_token(), niet nogmaals loggen

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE,
    }
    params = {"q": search_term, "limit": 50}

    try:
        response = requests.get(EBAY_SEARCH_URL, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        error = f"Kon eBay niet bereiken voor '{search_term}': {e}"
        print(f"[FOUT] {error}")
        return [], error
    except ValueError:
        error = f"eBay: geen geldige JSON voor '{search_term}'."
        print(f"[FOUT] {error}")
        return [], error

    raw_items = data.get("itemSummaries", [])
    items = [extract_ebay_fields(raw) for raw in raw_items]
    items = [i for i in items if i["id"] and i["title"] and i["price"] is not None]
    return items, None


def extract_ebay_fields(item):
    item_id = item.get("itemId")
    title = item.get("title") or ""

    price = None
    price_obj = item.get("price")
    if isinstance(price_obj, dict) and "value" in price_obj:
        try:
            price = float(price_obj["value"])
        except (TypeError, ValueError):
            price = None

    link = item.get("itemWebUrl")

    # 'nieuw' aanbod overslaan; we zoeken tweedehands
    condition = (item.get("condition") or "").lower()

    posted_at = None
    created = item.get("itemCreationDate")
    if created:
        try:
            posted_at = created[:16].replace("T", " ")
        except Exception:
            posted_at = None

    return {
        "id": f"ebay:{item_id}" if item_id else None,
        "title": title,
        "price": price,
        "link": link,
        "posted_at": posted_at,
        "source": "eBay",
        "_condition": condition,
    }


# ---------------------------------------------------------------------------
# Bron 3: Milanuncios (EXPERIMENTEEL — zie module-docstring)
# ---------------------------------------------------------------------------

MILANUNCIOS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
}


def search_milanuncios(search_term):
    """Haalt de Milanuncios-zoekpagina op en leest de JSON die de site zelf
    al insluit in de pagina (__NEXT_DATA__, standaard bij Next.js-sites).
    Geeft (genormaliseerde items, foutmelding) terug.

    LET OP: de exacte structuur van deze JSON kon niet vooraf getest
    worden. Als de paden hieronder niet kloppen, print deze functie een
    [WAARSCHUWING] met de top-level velden die WEL gevonden zijn — gebruik
    dat om de paden hieronder te herstellen (zie README.md)."""
    params = {"s": search_term, "orden": "fecha"}
    try:
        response = requests.get(
            MILANUNCIOS_SEARCH_URL, headers=MILANUNCIOS_HEADERS, params=params, timeout=20
        )
        if response.status_code != 200:
            error = f"Milanuncios gaf status {response.status_code} voor '{search_term}'."
            print(f"[FOUT] {error}")
            return [], error
        raw_html = response.text
    except requests.RequestException as e:
        error = f"Kon Milanuncios niet bereiken voor '{search_term}': {e}"
        print(f"[FOUT] {error}")
        return [], error

    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw_html, re.DOTALL
    )
    if not match:
        error = (
            f"Geen __NEXT_DATA__ blok gevonden op de Milanuncios-pagina voor "
            f"'{search_term}'. De site heeft mogelijk haar structuur aangepast, "
            "of blokkeert dit verzoek (bv. cookie-muur)."
        )
        print(f"[WAARSCHUWING] {error}")
        return [], error

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        error = f"__NEXT_DATA__ op Milanuncios kon niet als JSON gelezen worden voor '{search_term}'."
        print(f"[WAARSCHUWING] {error}")
        return [], error

    page_props = data.get("props", {}).get("pageProps", {})

    # We proberen een paar waarschijnlijke paden naar de advertentielijst.
    raw_items = None
    for path in (
        ["ads"],
        ["listings"],
        ["items"],
        ["searchResult", "ads"],
        ["searchResults", "items"],
        ["initialState", "ads"],
        ["adList"],
    ):
        node = page_props
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                node = None
                break
        if isinstance(node, list) and node:
            raw_items = node
            break

    if raw_items is None:
        available = list(page_props.keys())
        error = (
            f"Onbekende Milanuncios JSON-structuur voor '{search_term}'. "
            f"Beschikbare velden in pageProps: {available}"
        )
        print(f"[WAARSCHUWING] {error}")
        return [], error

    items = [extract_milanuncios_fields(raw) for raw in raw_items]
    items = [i for i in items if i["id"] and i["title"] and i["price"] is not None]
    return items, None


def extract_milanuncios_fields(item):
    """Normaliseert 1 ruw Milanuncios-item. Probeert meerdere waarschijnlijke
    veldnamen, aangezien de exacte structuur niet vooraf getest kon worden."""
    item_id = item.get("id") or item.get("adId") or item.get("itemId")
    title = item.get("title") or item.get("titulo") or ""

    price = None
    price_obj = item.get("price")
    if isinstance(price_obj, dict):
        price = price_obj.get("amount") or price_obj.get("value")
    elif isinstance(price_obj, (int, float, str)):
        try:
            price = float(str(price_obj).replace("€", "").replace(",", ".").strip())
        except ValueError:
            price = None

    slug_or_url = item.get("url") or item.get("urlList") or item.get("slug")
    if slug_or_url and slug_or_url.startswith("http"):
        link = slug_or_url
    elif slug_or_url:
        link = f"https://www.milanuncios.com{slug_or_url if slug_or_url.startswith('/') else '/' + slug_or_url}"
    else:
        link = None

    posted_at = None
    created = item.get("publicationDate") or item.get("fecha") or item.get("date")
    if created:
        try:
            posted_at = str(created)[:16].replace("T", " ")
        except Exception:
            posted_at = None

    return {
        "id": f"milanuncios:{item_id}" if item_id else None,
        "title": title,
        "price": float(price) if price is not None else None,
        "link": link,
        "posted_at": posted_at,
        "source": "Milanuncios",
    }


# ---------------------------------------------------------------------------
# Filteren en matchen
# ---------------------------------------------------------------------------

def matches_gpu(title, gpu_config):
    title_lower = title.lower()
    for kw in gpu_config.get("match_keywords", []):
        if kw.lower() not in title_lower:
            return False
    for kw in gpu_config.get("exclude_keywords", []):
        if kw.lower() in title_lower:
            return False
    return True


# ---------------------------------------------------------------------------
# Marktprijs-intelligentie
# ---------------------------------------------------------------------------

def compute_market_stats(seen_ads, gpu_name, min_samples):
    """Berekent de mediaanprijs van alle ooit geziene advertenties (van alle
    bronnen samen) voor deze GPU. Geeft None terug als er te weinig data is."""
    prices = [
        entry["price"]
        for entry in seen_ads.values()
        if entry.get("gpu_name") == gpu_name and isinstance(entry.get("price"), (int, float))
    ]
    if len(prices) < min_samples:
        return None
    return {
        "median": statistics.median(prices),
        "count": len(prices),
        "min": min(prices),
        "max": max(prices),
    }


def determine_thresholds(gpu_config, market_stats, pricing_config):
    """Geeft (effectieve_max_inkoopprijs, near_miss_bovengrens, scam_drempel,
    voorgestelde_verkoopprijs) terug."""
    static_max = gpu_config["max_price"]
    scam_threshold = None
    suggested_sell = None
    effective_max = static_max

    if market_stats:
        margin_pct = pricing_config.get("target_margin_percent", 30) / 100
        scam_pct = pricing_config.get("scam_threshold_percent", 50) / 100

        dynamic_max = round(market_stats["median"] * (1 - margin_pct))
        # De statische max_price uit config.json blijft een harde bovengrens;
        # de marktprijs kan het bod alleen strenger maken, nooit ruimer.
        effective_max = min(static_max, dynamic_max)
        scam_threshold = round(market_stats["median"] * scam_pct)
        suggested_sell = round(market_stats["median"] * 0.95)

    near_miss_pct = pricing_config.get("near_miss_margin_percent", 15) / 100
    near_miss_ceiling = round(effective_max * (1 + near_miss_pct))

    return effective_max, near_miss_ceiling, scam_threshold, suggested_sell


# ---------------------------------------------------------------------------
# Een GPU controleren over alle bronnen
# ---------------------------------------------------------------------------

def check_gpu(gpu_config, seen_ads, pricing_config, ebay_token, enabled_sources):
    hits = []
    near_misses = []
    suspicious = []
    errors = []

    all_items = []

    if "wallapop" in enabled_sources:
        items, error = search_wallapop(gpu_config["search_term"])
        if error:
            errors.append(error)
        all_items.extend(items)

    if "ebay" in enabled_sources:
        items, error = search_ebay(gpu_config["search_term"], ebay_token)
        if error:
            errors.append(error)
        # nieuwe (niet-tweedehands) eBay-aanbiedingen overslaan
        items = [i for i in items if "new" not in i.get("_condition", "")]
        all_items.extend(items)

    if "milanuncios" in enabled_sources:
        items, error = search_milanuncios(gpu_config["search_term"])
        if error:
            errors.append(error)
        all_items.extend(items)

    # Marktprijs berekenen VOORDAT we de nieuwe items toevoegen aan seen_ads,
    # zodat de drempel gebaseerd is op eerder verzamelde data.
    market_stats = compute_market_stats(seen_ads, gpu_config["name"], pricing_config.get("min_samples_for_market_price", 5))
    effective_max, near_miss_ceiling, scam_threshold, suggested_sell = determine_thresholds(
        gpu_config, market_stats, pricing_config
    )

    min_price = gpu_config.get("min_price", 0)

    for fields in all_items:
        if not matches_gpu(fields["title"], gpu_config):
            continue

        if fields["price"] < min_price:
            continue  # waarschijnlijk een accessoire/los onderdeel, helemaal negeren

        ad_key = fields["id"]
        if ad_key in seen_ads:
            continue  # al eerder gezien, niet opnieuw melden

        seen_ads[ad_key] = {
            "title": fields["title"],
            "price": fields["price"],
            "gpu_name": gpu_config["name"],
            "source": fields["source"],
            "first_seen": datetime.now().isoformat(timespec="seconds"),
        }

        fields["gpu_name"] = gpu_config["name"]
        fields["found_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        fields["market_median"] = market_stats["median"] if market_stats else None
        fields["suggested_sell_price"] = suggested_sell
        fields["market_sample_count"] = market_stats["count"] if market_stats else 0
        fields["min_samples_needed"] = pricing_config.get("min_samples_for_market_price", 5)

        if scam_threshold is not None and fields["price"] < scam_threshold:
            fields["reason"] = (
                f"Onder {pricing_config.get('scam_threshold_percent', 50)}% van de "
                f"marktmediaan (€{market_stats['median']:.0f}) — waarschijnlijk kapot/nep, controleer goed."
            )
            suspicious.append(fields)
        elif fields["price"] <= effective_max:
            fields["reason"] = f"Onder je inkoopdrempel van €{effective_max}."
            hits.append(fields)
        elif fields["price"] <= near_miss_ceiling:
            over_budget = fields["price"] - effective_max
            fields["reason"] = (
                f"€{over_budget:.0f} boven je drempel van €{effective_max}, maar binnen "
                f"de {pricing_config.get('near_miss_margin_percent', 15)}%-marge — misschien onderhandelbaar."
            )
            near_misses.append(fields)
        # anders: duidelijk te duur, genegeerd — geen melding

    return hits, near_misses, suspicious, errors


# ---------------------------------------------------------------------------
# Discord-melding
# ---------------------------------------------------------------------------

CATEGORY_STYLE = {
    "hit": {"emoji": "🟢", "label": "Koopje", "color": 5763719},  # groen
    "near_miss": {"emoji": "🟡", "label": "Net erboven", "color": 16776960},  # geel
    "suspicious": {"emoji": "🔴", "label": "Verdacht", "color": 15158332},  # rood
}


def build_embed(hit, category="hit"):
    style = CATEGORY_STYLE[category]
    price = hit["price"]

    sell_price = hit.get("suggested_sell_price")
    if sell_price:
        margin_euro = sell_price - price
        margin_pct = (margin_euro / price * 100) if price else 0
        sell_value = f"€{sell_price:.0f}"
        margin_value = f"€{margin_euro:.0f}  ({margin_pct:.0f}%)"
    else:
        count = hit.get("market_sample_count", 0)
        needed = hit.get("min_samples_needed", 5)
        sell_value = f"onbekend ({count}/{needed} adv.)"
        margin_value = "—"

    short_title = hit["title"] if len(hit["title"]) <= 100 else hit["title"][:97] + "…"

    embed = {
        "title": f"{style['emoji']} {hit['gpu_name']} — €{price:.0f} · {hit['source']}",
        "description": short_title,
        "color": style["color"],
        "fields": [
            {"name": "💰 Inkoop", "value": f"€{price:.0f}", "inline": True},
            {"name": "📈 Verkoop richtprijs", "value": sell_value, "inline": True},
            {"name": "💵 Marge", "value": margin_value, "inline": True},
            {"name": "📅 Geplaatst", "value": hit["posted_at"] or "onbekend", "inline": True},
        ],
        "footer": {"text": f"{style['label']} · {hit['reason']}"},
    }
    if hit["link"]:
        embed["url"] = hit["link"]
    return embed


def send_discord_notifications(hit_category_pairs):
    """Stuurt alle meldingen in batches van maximaal 10 embeds per Discord-
    bericht (Discord's limiet), met een korte pauze ertussen om de
    rate-limit (HTTP 429) te vermijden. Print alles ook altijd naar de log."""
    for hit, category in hit_category_pairs:
        print_hit(hit, category)

    if not DISCORD_WEBHOOK_URL:
        print(
            "[WAARSCHUWING] Geen DISCORD_WEBHOOK_URL ingesteld — Discord-"
            "meldingen worden overgeslagen (alleen hierboven in de log getoond)."
        )
        return

    CHUNK_SIZE = 10
    chunks = [
        hit_category_pairs[i : i + CHUNK_SIZE]
        for i in range(0, len(hit_category_pairs), CHUNK_SIZE)
    ]

    for i, chunk in enumerate(chunks):
        payload = {"embeds": [build_embed(hit, category) for hit, category in chunk]}
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
            response.raise_for_status()
            print(f"[OK] Discord-bericht {i + 1}/{len(chunks)} verstuurd ({len(chunk)} advertenties).")
        except requests.RequestException as e:
            print(f"[FOUT] Kon Discord-bericht {i + 1}/{len(chunks)} niet versturen: {e}")

        if i < len(chunks) - 1:
            time.sleep(1.5)  # kleine pauze tussen berichten tegen rate-limiting


def print_hit(hit, category="hit"):
    style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["hit"])
    sell = f"€{hit['suggested_sell_price']:.0f}" if hit.get("suggested_sell_price") else "onbekend"
    print("=" * 60)
    print(f"{style['emoji']} {style['label']} — {hit['gpu_name']} ({hit['source']})")
    print(f"Titel:      {hit['title']}")
    print(f"Inkoop:     €{hit['price']:.0f}    Verkoop: {sell}")
    print(f"Geplaatst:  {hit['posted_at'] or 'onbekend'}")
    print(f"Link:       {hit['link'] or 'onbekend'}")
    print(f"Waarom:     {hit['reason']}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# GitHub Pages dashboard
# ---------------------------------------------------------------------------

def render_dashboard(config, hits_log, status):
    """Schrijft docs/index.html — een overzicht van alle gevonden advertenties,
    bedoeld om via GitHub Pages als gratis webpagina gehost te worden."""
    DOCS_DIR.mkdir(exist_ok=True)

    gpu_names = ", ".join(html.escape(g["name"]) for g in config["gpus"])
    sources = ", ".join(html.escape(s) for s in config.get("sources", []))

    counts = {"hit": 0, "near_miss": 0, "suspicious": 0}
    for entry in hits_log:
        counts[entry.get("category", "hit")] = counts.get(entry.get("category", "hit"), 0) + 1

    if status["last_error"]:
        status_html = (
            f'<div class="status status-error">⚠ Laatste run gaf een fout: '
            f'{html.escape(status["last_error"])}</div>'
        )
    else:
        status_html = '<div class="status status-ok">✓ Laatste run was zonder fouten</div>'

    if hits_log:
        rows = []
        for idx, entry in enumerate(reversed(hits_log)):  # nieuwste eerst
            ad_id = entry.get("id") or f"idx-{idx}"
            category = entry.get("category", "hit")
            style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["hit"])
            badge_class = f"badge-{category}"
            title = html.escape(entry["title"])
            link = entry.get("link")
            link_html = (
                f'<a href="{html.escape(link)}" target="_blank">Bekijk advertentie →</a>'
                if link
                else "Link onbekend"
            )
            sell_html = ""
            if entry.get("suggested_sell_price"):
                margin_euro = entry["suggested_sell_price"] - entry["price"]
                margin_pct = (margin_euro / entry["price"] * 100) if entry["price"] else 0
                sell_html = (
                    f'<div class="meta">Verkoop richtprijs: €{entry["suggested_sell_price"]:.0f} '
                    f'&nbsp;·&nbsp; Marge: €{margin_euro:.0f} ({margin_pct:.0f}%)</div>'
                )
            rows.append(
                f"""
                <div class="card {badge_class}" data-ad-id="{html.escape(ad_id)}">
                    <div class="card-header">
                        <span class="tag">{html.escape(entry.get("gpu_name", ""))}</span>
                        <span class="tag source-tag">{html.escape(entry.get("source", ""))}</span>
                        <span class="price">€{entry["price"]:.0f}</span>
                    </div>
                    <div class="title">{title}</div>
                    <div class="meta">
                        Geplaatst: {html.escape(entry.get("posted_at") or "onbekend")}
                        &nbsp;·&nbsp;
                        Gevonden: {html.escape(entry.get("found_at", ""))}
                    </div>
                    {sell_html}
                    <div class="reason">{html.escape(entry.get("reason", ""))}</div>
                    <div class="card-footer">
                        <div class="link">{link_html}</div>
                        <div class="card-actions">
                            <button class="btn-archive" onclick="archiveAd('{html.escape(ad_id)}')">📥 Archiveren</button>
                            <button class="btn-delete" onclick="deleteAd('{html.escape(ad_id)}')">🗑️ Verwijderen</button>
                        </div>
                    </div>
                </div>
                """
            )
        cards_html = "\n".join(rows)
    else:
        cards_html = '<div class="empty">Nog geen advertenties gevonden. Deze pagina wordt elke run bijgewerkt.</div>'

    page = f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="300">
<title>GPU-monitor</title>
<style>
    :root {{
        --bg: #0f1115; --card-bg: #1a1d24; --border: #2a2e37; --text: #e6e8eb;
        --text-dim: #8b8f99; --green: #4ade80; --green-bg: #1f3d2c;
        --yellow: #facc15; --yellow-bg: #3d3410; --red: #f87171; --red-bg: #3d1f1f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
        background: var(--bg); color: var(--text);
        font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
        max-width: 780px; margin: 0 auto; padding: 32px 20px 60px;
    }}
    h1 {{ font-size: 22px; margin-bottom: 4px; }}
    .subtitle {{ color: var(--text-dim); font-size: 14px; margin-bottom: 20px; }}
    .status {{ padding: 10px 14px; border-radius: 8px; font-size: 14px; margin-bottom: 8px; }}
    .status-ok {{ background: var(--green-bg); color: var(--green); }}
    .status-error {{ background: var(--red-bg); color: var(--red); }}
    .counts {{ display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; font-size: 13px; }}
    .count-pill {{ padding: 4px 10px; border-radius: 999px; }}
    .count-hit {{ background: var(--green-bg); color: var(--green); }}
    .count-near_miss {{ background: var(--yellow-bg); color: var(--yellow); }}
    .count-suspicious {{ background: var(--red-bg); color: var(--red); }}
    .meta-bar {{
        display: flex; justify-content: space-between; color: var(--text-dim);
        font-size: 13px; margin-bottom: 20px; flex-wrap: wrap; gap: 6px;
    }}
    .card {{
        background: var(--card-bg); border: 1px solid var(--border); border-left: 4px solid var(--border);
        border-radius: 10px; padding: 16px 18px; margin-bottom: 14px;
    }}
    .card.badge-hit {{ border-left-color: var(--green); }}
    .card.badge-near_miss {{ border-left-color: var(--yellow); }}
    .card.badge-suspicious {{ border-left-color: var(--red); }}
    .card-header {{ display: flex; justify-content: space-between; align-items: center; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }}
    .tag {{ background: var(--border); color: var(--text-dim); font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 999px; }}
    .source-tag {{ opacity: 0.8; }}
    .price {{ font-size: 20px; font-weight: 700; margin-left: auto; }}
    .title {{ font-size: 15px; margin-bottom: 6px; }}
    .meta {{ color: var(--text-dim); font-size: 12px; margin-bottom: 4px; }}
    .reason {{ font-size: 13px; color: var(--text-dim); margin: 8px 0; }}
    .link a {{ color: var(--green); text-decoration: none; font-size: 13px; }}
    .link a:hover {{ text-decoration: underline; }}
    .empty {{ color: var(--text-dim); text-align: center; padding: 40px 0; font-size: 14px; }}
    .card-footer {{
        display: flex; justify-content: space-between; align-items: center;
        margin-top: 10px; flex-wrap: wrap; gap: 8px;
    }}
    .card-actions {{ display: flex; gap: 6px; }}
    .card-actions button {{
        background: var(--border); color: var(--text-dim); border: none;
        border-radius: 6px; padding: 5px 10px; font-size: 12px; cursor: pointer;
    }}
    .card-actions button:hover {{ background: #363b47; color: var(--text); }}
    .card.is-archived {{ opacity: 0.55; }}
    .toolbar {{
        display: flex; justify-content: space-between; align-items: center;
        margin-bottom: 16px; flex-wrap: wrap; gap: 8px;
    }}
    .toolbar button {{
        background: var(--card-bg); color: var(--text-dim); border: 1px solid var(--border);
        border-radius: 8px; padding: 6px 12px; font-size: 13px; cursor: pointer;
    }}
    .toolbar button:hover {{ color: var(--text); border-color: var(--text-dim); }}
    .toolbar button.active {{ color: var(--green); border-color: var(--green); }}
</style>
</head>
<body>
    <h1>🎮 GPU-monitor</h1>
    <div class="subtitle">Bewaakt: {gpu_names} · bronnen: {sources}</div>
    {status_html}
    <div class="counts">
        <span class="count-pill count-hit">{counts.get('hit', 0)} koopjes</span>
        <span class="count-pill count-near_miss">{counts.get('near_miss', 0)} net erboven</span>
        <span class="count-pill count-suspicious">{counts.get('suspicious', 0)} verdacht</span>
    </div>
    <div class="meta-bar">
        <span>Laatste run: {html.escape(status["last_check"])}</span>
        <span>Ververst elke 5 minuten</span>
    </div>
    <div class="toolbar">
        <button id="toggle-archived" onclick="toggleArchivedView()">📥 Toon gearchiveerde (<span id="archived-count">0</span>)</button>
        <button onclick="resetAll()">↺ Herstel alles (ongedaan maken)</button>
    </div>
    {cards_html}

<script>
    const ARCHIVE_KEY = 'gpuMonitorArchivedIds';
    const DELETE_KEY = 'gpuMonitorDeletedIds';

    function getIds(key) {{
        try {{
            const raw = localStorage.getItem(key);
            return raw ? new Set(JSON.parse(raw)) : new Set();
        }} catch (e) {{
            return new Set();
        }}
    }}

    function saveIds(key, idSet) {{
        localStorage.setItem(key, JSON.stringify(Array.from(idSet)));
    }}

    function archiveAd(adId) {{
        const archived = getIds(ARCHIVE_KEY);
        archived.add(adId);
        saveIds(ARCHIVE_KEY, archived);
        applyState();
    }}

    function deleteAd(adId) {{
        if (!confirm('Deze advertentie definitief verbergen? Dit kan alleen ongedaan gemaakt worden via "Herstel alles".')) {{
            return;
        }}
        const deleted = getIds(DELETE_KEY);
        deleted.add(adId);
        saveIds(DELETE_KEY, deleted);
        applyState();
    }}

    function resetAll() {{
        if (!confirm('Alle archiverings- en verwijder-markeringen op dit apparaat wissen?')) {{
            return;
        }}
        localStorage.removeItem(ARCHIVE_KEY);
        localStorage.removeItem(DELETE_KEY);
        showArchived = false;
        applyState();
    }}

    let showArchived = false;

    function toggleArchivedView() {{
        showArchived = !showArchived;
        applyState();
    }}

    function applyState() {{
        const archived = getIds(ARCHIVE_KEY);
        const deleted = getIds(DELETE_KEY);
        const cards = document.querySelectorAll('.card');
        let archivedCount = 0;

        cards.forEach(card => {{
            const id = card.getAttribute('data-ad-id');
            if (deleted.has(id)) {{
                card.style.display = 'none';
                return;
            }}
            if (archived.has(id)) {{
                archivedCount++;
                card.classList.add('is-archived');
                card.style.display = showArchived ? '' : 'none';
            }} else {{
                card.classList.remove('is-archived');
                card.style.display = '';
            }}
        }});

        document.getElementById('archived-count').textContent = archivedCount;
        const toggleBtn = document.getElementById('toggle-archived');
        toggleBtn.classList.toggle('active', showArchived);
        toggleBtn.textContent = (showArchived ? '📤 Verberg gearchiveerde (' : '📥 Toon gearchiveerde (') + archivedCount + ')';
    }}

    document.addEventListener('DOMContentLoaded', applyState);
</script>
</body>
</html>
"""
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(page)


# ---------------------------------------------------------------------------
# Hoofdlogica: EEN run
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    seen_ads = load_json(SEEN_ADS_PATH, {})
    hits_log = load_json(HITS_LOG_PATH, [])
    pricing_config = config.get("pricing", {})
    enabled_sources = set(config.get("sources", ["wallapop"]))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] Controleren...")
    print(f"Bewaakte GPU's: {', '.join(g['name'] for g in config['gpus'])}")
    print(f"Bronnen: {', '.join(enabled_sources)}")

    ebay_token = None
    had_error = False  # harde fouten (bv. Wallapop/eBay-verzoek mislukt) -> rood kruisje
    all_errors = []

    if "ebay" in enabled_sources:
        ebay_token, token_error = get_ebay_token()
        if token_error:
            # Geen eBay-credentials ingesteld is een bewuste keuze, geen fout van de
            # monitor zelf — dus alleen een waarschuwing, geen rood kruisje.
            print(f"[WAARSCHUWING] {token_error}")

    total_hits = 0
    total_near_miss = 0
    total_suspicious = 0
    to_notify = []  # (hit, category) paren, verzameld voor gebundeld versturen

    for gpu_config in config["gpus"]:
        hits, near_misses, suspicious, errors = check_gpu(
            gpu_config, seen_ads, pricing_config, ebay_token, enabled_sources
        )
        if errors:
            had_error = True
            all_errors.extend(errors)
        for hit in hits:
            hit["category"] = "hit"
            to_notify.append((hit, "hit"))
            hits_log.append(hit)
            total_hits += 1
        for hit in near_misses:
            hit["category"] = "near_miss"
            to_notify.append((hit, "near_miss"))
            hits_log.append(hit)
            total_near_miss += 1
        for hit in suspicious:
            hit["category"] = "suspicious"
            to_notify.append((hit, "suspicious"))
            hits_log.append(hit)
            total_suspicious += 1

    send_discord_notifications(to_notify)

    save_json(SEEN_ADS_PATH, seen_ads)
    save_json(HITS_LOG_PATH, hits_log)
    render_dashboard(
        config,
        hits_log,
        {"last_check": now, "last_error": "; ".join(all_errors) if all_errors else None},
    )

    print(
        f"{total_hits} koopje(s), {total_near_miss} net-erboven melding(en), "
        f"{total_suspicious} verdachte advertentie(s)."
    )

    if had_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
