#!/usr/bin/env python3
"""Zara stok takibi + Telegram bildirimi — grup girdili jenerik sürüm.

Takip edilecek şeyler Telegram grubundan yönetilir: gruba atılan Zara ürün
linki veya paylaşılan favori listesi linki otomatik takibe alınır
(watchlist.json). Bot her turda tüm kaynaklardaki ürünlerin beden bazında
stok durumunu okur, önceki durumla (state.json) karşılaştırır ve stoğa yeni
giren ürün/bedenler için gruba fotoğraflı bildirim gönderir.

Grup komutları:
    /liste          takip edilenleri göster
    /sil <numara>   /liste çıktısındaki numarayla takipten çıkar
    <zara linki>    takibe al (ürün linki ?v1= içermeli — kopyalanan
                    linklerde kendiliğinden bulunur)

Kullanım:
    python3 checker.py            # tek tur kontrol (cron / systemd timer için)
    python3 checker.py --loop     # sürekli çalışır, CHECK_INTERVAL_MIN aralıkla
    python3 checker.py --test     # gruba örnek bildirim atar, kontrol yapmaz
    python3 checker.py --dry-run  # kontrol eder ama Telegram'a mesaj atmaz
"""

import argparse
import html as html_mod
import json
import logging
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
WATCHLIST_FILE = BASE_DIR / "watchlist.json"
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "zara-watcher.log"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}

AVAILABLE_DEFAULT = {"in_stock", "low_on_stock"}
VIEW_PAYLOAD_MARKER = "window.zara.viewPayload = "
MAX_CONSECUTIVE_FAILURES = 5
MAX_SLOTS = 10  # rezerve favori listesi yuvası: /zara_liste1 ... /zara_liste10

PRODUCTS_DETAILS_API = "https://www.zara.com/tr/tr/products-details"
REFERENCE_SEARCH_API = "https://www.zara.com/itxrest/1/search/store/11766/reference"
WISHLIST_RE = re.compile(r"https://www\.zara\.com/[a-z]{2}/[a-z]{2}/user/share/wishlist/[\w-]+[^\s]*")
PRODUCT_RE = re.compile(r"https://www\.zara\.com/[a-z]{2}/[a-z]{2}/[\w-]+-p(\d+)\.html\?[^\s]*?v1=(\d+)[^\s]*")
BARE_PRODUCT_RE = re.compile(r"https://www\.zara\.com/[a-z]{2}/[a-z]{2}/[\w-]+-p(\d+)\.html[^\s]*")
# Mango ürün linki: /p/<bölüm>/<...>/<slug>/<ürün no>/<renk>/00
MANGO_PRODUCT_RE = re.compile(
    r"https://shop\.mango\.com/[a-z]{2}/[a-z]{2}/p/([^/\s]+)/[^\s]*?/(\d+)/(\w+)/\d+")
MANGO_SIZE_RE = re.compile(
    r'sizeSelector\.size(Available|Unavailable)\.\d+"[^>]*>.*?'
    r'<span class="textActionM[^"]*">([^<]+)</span>', re.S)

log = logging.getLogger("zara-watcher")


# ---------------------------------------------------------------- yapılandırma

def load_env():
    """Basit .env okuyucu — dış bağımlılık istemesin diye elle yazıldı."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    # Ortam değişkenleri .env'i ezer (GitHub Actions secrets böyle gelir)
    import os
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CHECK_INTERVAL_MIN"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def _load_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("beklenen formatta değil")
        return data
    except FileNotFoundError:
        return fallback
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("%s okunamadı (%s) — varsayılanla devam", path.name, exc)
        return fallback


def _save_json(path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def load_state():
    return _load_json(STATE_FILE, {})


def save_state(state):
    _save_json(STATE_FILE, state)


def load_watchlist(config):
    """watchlist.json'u yükler; yoksa config'deki wishlist ile başlatır."""
    fallback = {"wishlists": [], "products": []}
    if config.get("wishlist_url"):
        fallback["wishlists"].append({
            "slot": 1,
            "url": config["wishlist_url"],
            "label": "Kurulum listesi",
            "added_by": "kurulum",
        })
    data = _load_json(WATCHLIST_FILE, fallback)
    # Eski kayıtlarda slot yoksa boş yuvalara sırayla ata
    used = {w.get("slot") for w in data["wishlists"] if w.get("slot")}
    next_free = 1
    for w in data["wishlists"]:
        if not w.get("slot"):
            while next_free in used:
                next_free += 1
            w["slot"] = next_free
            used.add(next_free)
    return data


def save_watchlist(watchlist):
    _save_json(WATCHLIST_FILE, watchlist)


# ------------------------------------------------------------------- zara http

def _zara_get(url, params=None, accept_json=False):
    """3 deneme, 2/4/8 sn backoff. 403/429'da tur atlanır."""
    headers = dict(HEADERS)
    if accept_json:
        headers["Accept"] = "application/json"
        headers["Referer"] = "https://www.zara.com/tr/tr/"
    last_exc = None
    for attempt, delay in enumerate((0, 2, 4), start=1):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code in (403, 429):
                raise RuntimeError(f"Zara isteği engellendi (HTTP {resp.status_code})")
            resp.raise_for_status()
            return resp
        except (requests.RequestException, RuntimeError) as exc:
            last_exc = exc
            log.warning("İstek başarısız (deneme %d/3): %s", attempt, exc)
    raise RuntimeError(f"Zara isteği başarısız: {last_exc}")


def fetch_products_details(v1_ids):
    """products-details endpoint'inden ham ürün listesi döndürür (toplu)."""
    results = []
    ids = [str(v) for v in v1_ids]
    for i in range(0, len(ids), 10):  # 10'arlı gruplar halinde
        chunk = ids[i:i + 10]
        params = [("productIds", pid) for pid in chunk] + [("ajax", "true")]
        resp = _zara_get(PRODUCTS_DETAILS_API, params=params, accept_json=True)
        results.extend(resp.json())
        if i + 10 < len(ids):
            time.sleep(random.uniform(3, 6))
    return results


def resolve_v1_from_seo_pid(seo_pid):
    """v1'siz ürün linkindeki numaradan (p02554380) rengin productId'sini bulur.

    Numara şeması: 8 hane = [sezon hanesi][4 hane model]/[3 hane kalite]
    → referans "2554/380" türetilir, itxrest referans aramasıyla ürün bulunur.
    Dönüş: (v1, renk_sayısı) veya (None, 0).
    """
    if len(seo_pid) != 8 or not seo_pid.isdigit():
        return None, 0
    reference = f"{seo_pid[1:5]}/{seo_pid[5:]}"
    resp = _zara_get(REFERENCE_SEARCH_API,
                     params={"reference": reference, "locale": "tr_TR",
                             "ajax": "true"},
                     accept_json=True)
    for result in (resp.json().get("results") or []):
        content = result.get("content") or {}
        detail = content.get("detail") or {}
        if detail.get("displayReference") != reference:
            continue
        colors = detail.get("colors") or []
        if colors and colors[0].get("productId"):
            return colors[0]["productId"], len(colors)
    return None, 0


def _color_entry(raw, color, name=None):
    """Ham ürün + renk verisinden state'e yazılacak kaydı üretir."""
    seo = raw.get("seo") or {}
    seo_pid = seo.get("seoProductId") or str(raw.get("id", ""))
    keyword = seo.get("keyword", "urun")
    v1 = color.get("productId") or seo.get("discernProductId") or ""
    url = f"https://www.zara.com/tr/tr/{keyword}-p{seo_pid}.html?v1={v1}"

    image = ""
    medias = color.get("xmedia") or raw.get("xmedia") or []
    if medias and medias[0].get("url"):
        image = medias[0]["url"].replace("{width}", "800")

    sizes = {}
    for size in color.get("sizes") or []:
        sname = str(size.get("name", "")).strip()
        if sname:
            sizes[sname] = size.get("availability", "unknown")
    if not sizes:
        sizes["STANDART"] = "unknown"

    detail = raw.get("detail") or {}
    key = f"{seo_pid}-{color.get('id', '')}"
    return key, {
        "name": name or raw.get("name") or "?",
        "ref": detail.get("displayReference", ""),
        "color": color.get("name", ""),
        "kind": raw.get("kind", ""),
        "family": raw.get("familyName", ""),
        "section": raw.get("sectionName", ""),
        "price": color.get("price"),
        "old_price": color.get("oldPrice"),
        "discount": color.get("displayDiscountPercentage"),
        "image": image,
        "url": url,
        "sizes": sizes,
    }


def snapshot_from_product(raw, wanted_v1=None):
    """Tek ürünün (istenen rengiyle) anlık görüntüsü."""
    colors = (raw.get("detail") or {}).get("colors") or []
    if not colors:
        return {}
    color = next((c for c in colors if str(c.get("productId")) == str(wanted_v1)),
                 colors[0])
    key, entry = _color_entry(raw, color)
    return {key: entry}


def fetch_wishlist_snapshot(url):
    """Paylaşılan wishlist sayfasından tüm ürünlerin anlık görüntüsü."""
    html = _zara_get(url).text
    idx = html.find(VIEW_PAYLOAD_MARKER)
    if idx < 0:
        raise RuntimeError(
            "Sayfada viewPayload bulunamadı — Zara sayfa yapısını değiştirmiş olabilir"
        )
    payload, _ = json.JSONDecoder().raw_decode(html, idx + len(VIEW_PAYLOAD_MARKER))
    items = (payload.get("wishlist") or {}).get("items") or []
    if not items:
        raise RuntimeError("Wishlist boş döndü — link geçersiz olabilir")

    snapshot = {}
    for item in items:
        product = item.get("product") or {}
        raw = item.get("unprocessedProduct") or {}
        colors = (raw.get("detail") or {}).get("colors") or []
        wanted_color = (product.get("color") or {}).get("id")
        color = next((c for c in colors if c.get("id") == wanted_color), None) \
            or (colors[0] if colors else None)
        if color is None:
            # Beden/renk verisi hiç yoksa ürün seviyesindeki bayraklardan türet
            color = {"id": (product.get("color") or {}).get("id", ""),
                     "sizes": [{"name": "STANDART",
                                "availability": "coming_soon" if product.get("isComingSoon")
                                else ("out_of_stock" if product.get("isOutOfStock")
                                      else "in_stock")}]}
        key, entry = _color_entry(raw, color, name=product.get("name"))
        snapshot[key] = entry
    return snapshot


# ----------------------------------------------------------------------- mango

MANGO_SECTION_MAP = {"kadın": "WOMAN", "kadin": "WOMAN", "erkek": "MAN",
                     "cocuk": "KID", "çocuk": "KID", "bebek": "KID"}


def fetch_mango_product(url, gender_slug, garment_id, color_code):
    """Mango ürün sayfasından state kaydı üretir. Dönüş: (key, entry)."""
    page = _zara_get(url).text

    sizes = {}
    for m in MANGO_SIZE_RE.finditer(page):
        name = html_mod.unescape(m.group(2)).strip()
        if name:
            sizes[name] = "in_stock" if m.group(1) == "Available" else "out_of_stock"
    if not sizes:
        sizes["STANDART"] = "unknown"

    title = re.search(r"<title>([^<]+)</title>", page)
    name = html_mod.unescape(title.group(1)).split(" - ")[0].strip() if title else "?"

    prices = re.findall(r'itemProp="price" content="([\d.]+)"', page)
    price = old_price = None
    if len(prices) >= 2:            # ilki üstü çizili eski fiyat
        old_price = int(float(prices[0]) * 100)
        price = int(float(prices[1]) * 100)
    elif prices:
        price = int(float(prices[0]) * 100)
    disc = re.search(r'discountRate"><span[^>]*>-%(\d+)</span>', page)

    img = re.search(r'property="og:image" content="([^"]+)"', page)
    color = re.search(r'"color"\s*:\s*"([^"]+)"', page)

    slug = gender_slug.lower()
    entry = {
        "name": name.upper(),
        "ref": f"{garment_id}/{color_code}",
        "color": html_mod.unescape(color.group(1)) if color else "",
        "kind": "Wear",
        "family": "AYAKKABI" if "/ayakkab" in unquote(url).lower() else "",
        "section": MANGO_SECTION_MAP.get(slug, "WOMAN"),
        "price": price,
        "old_price": old_price,
        "discount": int(disc.group(1)) if disc else None,
        "image": img.group(1) if img else "",
        "url": url,
        "sizes": sizes,
    }
    return f"mango-{garment_id}-{color_code}", entry


def _handle_mango_link(env, config, state, watchlist, match, sender):
    gender_slug, garment_id, color_code = match.groups()
    url = match.group(0)
    mkey = f"mango-{garment_id}-{color_code}"
    if any(p.get("key") == mkey for p in watchlist["products"]):
        send_telegram(env, "ℹ️ Bu ürün zaten takipte.", disable_preview=True)
        return False
    try:
        key, entry = fetch_mango_product(url, gender_slug, garment_id, color_code)
    except RuntimeError as exc:
        log.error("Mango ürünü doğrulanamadı (%s): %s", garment_id, exc)
        send_telegram(env, "⚠️ Mango ürün bilgisi alınamadı, sonraki turda "
                           "tekrar deneyin.", disable_preview=True)
        return False
    watchlist["products"].append({
        "store": "mango", "key": key, "url": url,
        "gender_slug": gender_slug, "garment_id": garment_id,
        "color_code": color_code,
        "name": f"{entry['name']}" + (f" ({entry['color']})" if entry["color"] else ""),
        "added_by": sender, "added_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    state.setdefault("products", {})[key] = entry

    in_stock = [s for s, a in entry["sizes"].items()
                if a in AVAILABLE_DEFAULT and size_allowed(config, entry, s)]
    send_telegram(env, f"✅ Takibe alındı (Mango): {entry['name']}\n"
                       f"Şu an stokta (kurallara uyan bedenler): "
                       f"{', '.join(in_stock) if in_stock else 'yok'}\n"
                       "Yeni beden stoğa girince haber veririm.",
                  disable_preview=True)
    log.info("Gruptan Mango ürünü eklendi (%s): %s", sender, entry["name"])
    return True


# ------------------------------------------------------------------- karşılaştırma

LETTER_ORDER = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]


def size_allowed(config, entry, size_name):
    """Bir bedenin bildirime konu olup olmayacağına karar verir.

    Öncelik sırası:
    1. size_filters — ürün bazlı istisna (anahtar: display reference "5862/081"
       veya ürün adının bir parçası)
    2. limits — cinsiyete göre üst sınır kuralları:
       Ürün MAN bölümündeyse erkek sınırları, diğer her durumda (WOMAN,
       unisex, adında UNISEX geçen, bilinmeyen) kadın sınırları uygulanır.
       Harf bedenlerde letter_max, ayakkabıda shoe_max, rakamlı giyim
       bedenlerinde (jean vb.) pants_max üst sınırdır.
    3. Bedeni olmayan ürünler (parfüm, çanta — "STANDART") her zaman izlenir.
    """
    filters = config.get("size_filters") or {}
    for key, sizes in filters.items():
        if key == entry["ref"] or key.lower() in entry["name"].lower():
            return not sizes or size_name in sizes

    limits_cfg = config.get("limits") or {}
    section = entry.get("section", "").upper()
    is_man = section == "MAN" and "UNISEX" not in entry.get("name", "").upper()
    group = "MAN" if is_man else ("KID" if "KID" in section else "WOMAN")
    limits = limits_cfg.get(group)
    if not limits:
        return True

    # Çocuk ürünleri: sadece belirtilen yaş bedenleri (Zara formatı: "13/14 yaş (164 cm)")
    if group == "KID":
        allowed_ages = limits.get("only_sizes") or []
        if not allowed_ages:
            return True
        norm = size_name.upper().replace("-", "/").replace(" ", "")
        return any(a.upper().replace("-", "/").replace(" ", "") in norm
                   for a in allowed_ages)

    # "XS-S", "36/38" gibi kombine bedenlerde ilk parçaya göre karar ver
    token = re.split(r"[-/\s]", size_name.strip().upper())[0]

    if token in LETTER_ORDER:
        idx = LETTER_ORDER.index(token)
        lo = str(limits.get("letter_min", "")).upper()
        hi = str(limits.get("letter_max", "")).upper()
        if lo in LETTER_ORDER and idx < LETTER_ORDER.index(lo):
            return False
        if hi in LETTER_ORDER and idx > LETTER_ORDER.index(hi):
            return False
        return True

    if token.isdigit():
        number = int(token)
        if entry.get("family", "").upper() == "AYAKKABI":
            lo, hi = limits.get("shoe_min"), limits.get("shoe_max")
        else:
            lo, hi = limits.get("pants_min"), limits.get("pants_max")
        if lo is not None and number < int(lo):
            return False
        if hi is not None and number > int(hi):
            return False
        return True

    return True  # STANDART ve tanınmayan beden adları: sınırlama yok


def find_restocks(config, previous, current):
    """out_of_stock/coming_soon → in_stock/low_on_stock geçişlerini bulur."""
    available = set(AVAILABLE_DEFAULT)
    if not config.get("notify_low_on_stock", True):
        available.discard("low_on_stock")

    restocks = []
    for key, entry in current.items():
        prev_entry = previous.get(key)
        if prev_entry is None:
            continue  # yeni takibe alınan ürün: sadece kaydet, bildirme
        prev_sizes = prev_entry.get("sizes", {})
        newly = [
            size
            for size, avail in entry["sizes"].items()
            if avail in available
            and prev_sizes.get(size) is not None
            and prev_sizes.get(size) not in available
            and size_allowed(config, entry, size)
        ]
        if newly:
            restocks.append((entry, newly))
    return restocks


# --------------------------------------------------------------------- telegram

def _telegram_call(env, method, body, quiet=False):
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token or not env.get("TELEGRAM_CHAT_ID"):
        raise RuntimeError(
            ".env içinde TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID tanımlı olmalı"
        )
    api = f"https://api.telegram.org/bot{token}/{method}"
    last = None
    for attempt, delay in enumerate((0, 2, 4), start=1):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.post(api, json=body, timeout=35)
            data = resp.json()
            if data.get("ok"):
                return data.get("result")
            last = data
            if 400 <= resp.status_code < 500:
                break  # tekrar denemek anlamsız
        except (requests.RequestException, ValueError) as exc:
            last = exc
        if not quiet:
            log.warning("Telegram %s başarısız (deneme %d/3): %s", method, attempt, last)
    return None


def send_telegram(env, text, disable_preview=False, photo=None):
    """Bildirim gönderir; resim varsa sendPhoto, yoksa/başarısızsa sendMessage."""
    chat_id = env["TELEGRAM_CHAT_ID"]
    if photo:
        if _telegram_call(env, "sendPhoto",
                          {"chat_id": chat_id, "photo": photo, "caption": text}) is not None:
            return
        log.warning("sendPhoto başarısız, düz mesaja düşülüyor")
    if _telegram_call(env, "sendMessage",
                      {"chat_id": chat_id, "text": text,
                       "disable_web_page_preview": disable_preview}) is None:
        raise RuntimeError("Telegram mesajı gönderilemedi")


def format_price(kurus):
    """79000 → '790,00 TL' (Zara fiyatları kuruş cinsinden gelir)."""
    if kurus is None:
        return ""
    lira, kr = divmod(int(kurus), 100)
    return f"{lira:,}".replace(",", ".") + f",{kr:02d} TL"


def format_message(entry, sizes):
    color = f" ({entry['color']})" if entry.get("color") else ""
    price_line = ""
    if entry.get("price") is not None:
        price_line = f"Fiyat: {format_price(entry['price'])}"
        if entry.get("old_price"):
            price_line += f" (eski: {format_price(entry['old_price'])}"
            if entry.get("discount"):
                price_line += f", %{entry['discount']} indirim"
            price_line += ")"
        price_line += "\n"
    return (
        "🎉 STOKTA!\n\n"
        f"{entry['name']}{color}\n"
        f"Beden: {', '.join(sizes)}\n"
        f"{price_line}\n"
        f"{entry['url']}"
    )


# ------------------------------------------------- grup mesajlarından girdi alma

HELP_TEXT = (
    "🤖 Stok takip botu (Zara + Mango)\n\n"
    "Komutlar:\n"
    "/zara_liste1 <link> ... /zara_liste10 <link> — 10 favori listesi "
    "yuvası: yuva boşsa linki kaydeder, doluysa günceller. Linksiz "
    "yazınca (/zara_liste3) yuvanın durumunu gösterir.\n"
    "/ekle <link> — tekil ürün takibe al (Zara veya Mango; linkte renk "
    "yoksa ilk renk seçilir)\n"
    "/liste — tüm yuvaları ve tekil ürünleri göster\n"
    "/sil liste3 — 3. yuvayı boşalt | /sil 2 — 2. tekil ürünü çıkar\n\n"
    "Not: komutsuz atılan linkler takibe alınmaz."
)


def _watchlist_lines(watchlist):
    """/liste için döküm: 10 yuva (boşlar dahil) + tekil ürünler."""
    slots = {w.get("slot"): w for w in watchlist["wishlists"]}
    lines = []
    for n in range(1, MAX_SLOTS + 1):
        w = slots.get(n)
        if w:
            lines.append(f"Liste {n}: 📋 {w.get('label', 'Favori listesi')}"
                         f" (ekleyen: {w.get('added_by', '?')})")
        else:
            lines.append(f"Liste {n}: boş")
    lines.append("")
    if watchlist["products"]:
        lines.append("Tekil ürünler:")
        for i, pr in enumerate(watchlist["products"], 1):
            store = pr.get("store", "zara").capitalize()
            lines.append(f"{i}. 👕 [{store}] {pr.get('name', pr['url'])}"
                         f" (ekleyen: {pr.get('added_by', '?')})")
    else:
        lines.append("Tekil ürün yok.")
    return lines


def _handle_product_link(env, config, state, watchlist, v1, sender):
    if any(str(p["v1"]) == str(v1) for p in watchlist["products"]):
        send_telegram(env, "ℹ️ Bu ürün zaten takipte.", disable_preview=True)
        return False
    try:
        raw_list = fetch_products_details([v1])
    except RuntimeError as exc:
        log.error("Ürün doğrulanamadı (v1=%s): %s", v1, exc)
        send_telegram(env, "⚠️ Ürün bilgisi alınamadı, sonraki turda tekrar deneyin.",
                      disable_preview=True)
        return False
    if not raw_list:
        send_telegram(env, "⚠️ Bu linkten ürün bulunamadı. Linki Zara "
                           "uygulamasındaki/sitesindeki 'paylaş' ile kopyalayıp "
                           "olduğu gibi atmayı deneyin.", disable_preview=True)
        return False

    snap = snapshot_from_product(raw_list[0], wanted_v1=v1)
    if not snap:
        send_telegram(env, "⚠️ Ürünün beden bilgisi okunamadı.", disable_preview=True)
        return False
    key, entry = next(iter(snap.items()))
    watchlist["products"].append({
        "v1": int(v1), "url": entry["url"], "name":
            f"{entry['name']}" + (f" ({entry['color']})" if entry["color"] else ""),
        "added_by": sender, "added_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    # Hemen state'e yaz ki bir sonraki tur mevcut stoğu "yeni girdi" sanmasın
    state.setdefault("products", {})[key] = entry

    in_stock = [s for s, a in entry["sizes"].items()
                if a in AVAILABLE_DEFAULT and size_allowed(config, entry, s)]
    color = f" ({entry['color']})" if entry["color"] else ""
    msg = f"✅ Takibe alındı: {entry['name']}{color}\n"
    msg += f"Şu an stokta (kurallara uyan bedenler): {', '.join(in_stock) if in_stock else 'yok'}\n"
    msg += "Yeni beden stoğa girince haber veririm."
    send_telegram(env, msg, disable_preview=True)
    log.info("Gruptan ürün eklendi (%s): %s", sender, entry["name"])
    return True


def _handle_slot_command(env, state, watchlist, slot, text, sender):
    """/zara_listeN: yuva boşsa linki kaydeder, doluysa günceller."""
    if not 1 <= slot <= MAX_SLOTS:
        send_telegram(env, f"Liste numarası 1-{MAX_SLOTS} arasında olmalı.",
                      disable_preview=True)
        return False
    existing = next((w for w in watchlist["wishlists"] if w.get("slot") == slot), None)
    urls = WISHLIST_RE.findall(text)
    if not urls:
        if existing:
            send_telegram(env, f"Liste {slot}: {existing['label']} "
                               f"(ekleyen: {existing.get('added_by', '?')})\n"
                               f"Güncellemek için: /zara_liste{slot} <yeni link>",
                          disable_preview=True)
        else:
            send_telegram(env, f"Liste {slot} boş. Doldurmak için: "
                               f"/zara_liste{slot} <paylaşılan favori listesi linki>",
                          disable_preview=True)
        return False
    url = urls[0]
    if existing and existing["url"].split("?")[0] == url.split("?")[0]:
        send_telegram(env, f"ℹ️ Liste {slot} zaten bu linki takip ediyor.",
                      disable_preview=True)
        return False
    try:
        snap = fetch_wishlist_snapshot(url)
    except RuntimeError as exc:
        log.error("Wishlist doğrulanamadı (yuva %d): %s", slot, exc)
        send_telegram(env, f"⚠️ Liste okunamadı, yuva değiştirilmedi: {exc}",
                      disable_preview=True)
        return False
    watchlist["wishlists"] = [w for w in watchlist["wishlists"]
                              if w.get("slot") != slot]
    watchlist["wishlists"].append({
        "slot": slot, "url": url,
        "label": f"{sender} listesi ({len(snap)} ürün)",
        "added_by": sender, "added_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    watchlist["wishlists"].sort(key=lambda w: w.get("slot", MAX_SLOTS + 1))
    state.setdefault("products", {}).update(snap)
    verb = "güncellendi" if existing else "kaydedildi"
    send_telegram(env, f"📌 Liste {slot} {verb}: {len(snap)} ürün takipte.",
                  disable_preview=True)
    log.info("Yuva %d %s (%s): %d ürün", slot, verb, sender, len(snap))
    return True


def _process_links(env, config, state, watchlist, text, sender):
    """Metindeki ürün linklerini takibe alır. (değişti_mi, link_bulundu_mu) döner."""
    changed = found = False
    if WISHLIST_RE.search(text):
        found = True
        send_telegram(env, "ℹ️ Favori listeleri yuvalarla yönetiliyor: "
                           "/zara_liste1 ... /zara_liste10 <link>\n"
                           "Boş yuvaya kaydeder, dolu yuvayı günceller.",
                      disable_preview=True)

    for match in MANGO_PRODUCT_RE.finditer(unquote(text)):
        found = True
        changed |= _handle_mango_link(env, config, state, watchlist, match, sender)

    handled_pids = set()
    for match in PRODUCT_RE.finditer(text):
        found = True
        handled_pids.add(match.group(1))
        changed |= _handle_product_link(env, config, state, watchlist,
                                        match.group(2), sender)

    # v1 parametresi olmayan ürün linkleri: referans aramasıyla rengi çöz
    for match in BARE_PRODUCT_RE.finditer(text):
        seo_pid = match.group(1)
        if seo_pid in handled_pids or "v1=" in match.group(0):
            continue
        found = True
        try:
            v1, color_count = resolve_v1_from_seo_pid(seo_pid)
        except RuntimeError as exc:
            log.error("Referans araması başarısız (%s): %s", seo_pid, exc)
            v1 = None
            color_count = 0
        if v1 is None:
            send_telegram(env, "⚠️ Bu linkten ürün bulunamadı. Linki Zara "
                               "uygulaması/sitesindeki Paylaş düğmesiyle "
                               "kopyalayıp tekrar deneyin.", disable_preview=True)
            continue
        if color_count > 1:
            send_telegram(env, "ℹ️ Linkte renk belirtilmemiş; ürünün ilk rengi "
                               "takibe alınıyor. Başka bir renk istiyorsanız "
                               "linki Paylaş düğmesiyle kopyalayıp atın.",
                          disable_preview=True)
        changed |= _handle_product_link(env, config, state, watchlist, v1, sender)
    return changed, found


def _handle_command(env, config, state, watchlist, text, sender):
    cmd = text.split("@")[0].split() or [""]
    # Türkçe karakter ve büyük/küçük harf toleransı: /Yardım -> /yardim
    cmd[0] = (cmd[0].replace("İ", "i").lower()
              .replace("ı", "i").replace("ş", "s").replace("ü", "u"))

    slot_match = re.match(r"^/zara_?liste_?(\d{1,2})$", cmd[0])
    if slot_match:
        return _handle_slot_command(env, state, watchlist,
                                    int(slot_match.group(1)), text, sender)
    if cmd[0] == "/ekle":
        changed, found = _process_links(env, config, state, watchlist, text, sender)
        if not found:
            send_telegram(env, "Kullanım: /ekle <ürün linki>\n"
                               "Zara veya Mango ürün linki olabilir. Favori "
                               "listeleri için /zara_liste1 ... /zara_liste10.",
                          disable_preview=True)
        return changed
    if cmd[0] in ("/liste", "/list"):
        lines = _watchlist_lines(watchlist)
        send_telegram(env, "📌 Takip edilenler:\n" + "\n".join(lines),
                      disable_preview=True)
        return False
    if cmd[0] == "/sil":
        args = [a.lower() for a in cmd[1:]]
        # "/sil liste3" veya "/sil liste 3" → yuvayı boşalt
        slot_arg = re.match(r"^liste_?(\d{1,2})$", args[0]) if args else None
        if slot_arg or (len(args) >= 2 and args[0] == "liste" and args[1].isdigit()):
            n = int(slot_arg.group(1)) if slot_arg else int(args[1])
            existing = next((w for w in watchlist["wishlists"]
                             if w.get("slot") == n), None)
            if not existing:
                send_telegram(env, f"Liste {n} zaten boş.", disable_preview=True)
                return False
            watchlist["wishlists"] = [w for w in watchlist["wishlists"]
                                      if w.get("slot") != n]
            send_telegram(env, f"🗑 Liste {n} boşaltıldı: {existing['label']}",
                          disable_preview=True)
            return True
        # "/sil 2" → tekil ürünü çıkar
        try:
            idx = int(args[0]) - 1
            removed = watchlist["products"].pop(idx)
        except (IndexError, ValueError):
            send_telegram(env, "Kullanım: /sil liste3 (yuva boşaltır) veya "
                               "/sil 2 (tekil ürünü çıkarır) — numaralar için /liste",
                          disable_preview=True)
            return False
        send_telegram(env, f"🗑 Takipten çıkarıldı: "
                           f"{removed.get('name') or removed.get('url')}",
                      disable_preview=True)
        return True
    if cmd[0] == "/panel":
        panel_url = config.get("panel_url", "")
        send_telegram(env, "🛍 Yönetim paneli:\n"
                           f"{panel_url}\n\n"
                           "Kullanımı: linke dokunun → panelde işlemi yapın "
                           "(liste güncelle / ürün ekle / sil) → panel komutu "
                           "kopyalar → bu gruba yapıştırıp gönderin.\n"
                           "İpucu: bu mesajı grupta sabitleyin, panel hep "
                           "elinizin altında olur.",
                      disable_preview=True)
        return False
    if cmd[0] in ("/start", "/yardim", "/help"):
        send_telegram(env, HELP_TEXT, disable_preview=True)
    return False


def poll_group_messages(env, config, state, watchlist):
    """Gruba atılan linkleri/komutları işler. Watchlist değiştiyse True döner.

    Not: Botun grup mesajlarını görebilmesi için BotFather'da privacy mode
    kapalı olmalı (/setprivacy → Disable) ve bot gruba ondan sonra eklenmiş
    olmalı.
    """
    offset = int(state.get("tg_offset", 0))
    result = _telegram_call(env, "getUpdates",
                            {"offset": offset + 1, "timeout": 0,
                             "allowed_updates": ["message"]}, quiet=True)
    if result is None:
        log.warning("getUpdates alınamadı (başka bir kopya çalışıyor olabilir)")
        return False

    changed = False
    group_id = str(env["TELEGRAM_CHAT_ID"])
    allowed_users = set(config.get("allowed_users") or [])
    for update in result:
        state["tg_offset"] = max(int(state.get("tg_offset", 0)), update["update_id"])
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        text = msg.get("text") or ""
        sender = (msg.get("from") or {}).get("first_name", "?")
        user_id = (msg.get("from") or {}).get("id")

        if str(chat.get("id")) == group_id:
            if text.startswith("/"):
                changed |= _handle_command(env, config, state, watchlist, text, sender)
            # /ekle olmadan atılan linkler bilinçli olarak yok sayılır:
            # grupta sohbet ederken paylaşılan linkler takibe girmesin
            continue

        # Botla özel sohbet: sadece izinli kullanıcılar (panel / mini app)
        if chat.get("type") != "private" or user_id not in allowed_users:
            continue

        web_app = msg.get("web_app_data") or {}
        if web_app.get("data", "").startswith("/"):
            # Panelden gelen işlem: grup komutuyla aynı şekilde işle
            changed |= _handle_command(env, config, state, watchlist,
                                       web_app["data"], sender)
            _telegram_call(env, "sendMessage", {
                "chat_id": chat["id"],
                "text": "✅ Panel işlemi uygulandı — onay grup sohbetinde.",
            }, quiet=True)
            continue

        cmd0 = (text.split() or [""])[0].split("@")[0].lower() \
            .replace("ı", "i").replace("İ", "i")
        if cmd0 in ("/panel", "/start"):
            panel_url = config.get("panel_url")
            if not panel_url:
                continue
            _telegram_call(env, "sendMessage", {
                "chat_id": chat["id"],
                "text": "Panel düğmesi aşağıda 👇 Ona dokununca yönetim "
                        "paneli Telegram içinde açılır. Yaptığınız işlemler "
                        "bota iletilir ve ~15 dk içinde uygulanır.",
                "reply_markup": {
                    "keyboard": [[{"text": "🛍 Paneli Aç",
                                   "web_app": {"url": panel_url}}]],
                    "resize_keyboard": True,
                    "is_persistent": True,
                },
            }, quiet=True)
    return changed


# ------------------------------------------------------------------------- ana

def build_snapshot(state, watchlist):
    """Tüm kaynaklardan güncel durumu toplar. Kaynak bazlı hata izole edilir."""
    current = {}
    failures = state.setdefault("source_failures", {})
    any_success = False

    for wl in watchlist["wishlists"]:
        skey = "wl:" + wl["url"].split("?")[0]
        try:
            current.update(fetch_wishlist_snapshot(wl["url"]))
            failures.pop(skey, None)
            any_success = True
        except RuntimeError as exc:
            failures[skey] = failures.get(skey, 0) + 1
            log.error("Wishlist okunamadı (%d. kez): %s", failures[skey], exc)
        time.sleep(random.uniform(3, 6))

    zara_products = [p for p in watchlist["products"] if p.get("store", "zara") == "zara"]
    mango_products = [p for p in watchlist["products"] if p.get("store") == "mango"]

    v1_ids = [p["v1"] for p in zara_products]
    if v1_ids:
        try:
            raw_list = fetch_products_details(v1_ids)
            found = set()
            for raw in raw_list:
                for pr in zara_products:
                    snap = snapshot_from_product(raw, wanted_v1=pr["v1"])
                    for key, entry in snap.items():
                        if str(entry["url"].split("v1=")[-1]) == str(pr["v1"]):
                            current[key] = entry
                            found.add(str(pr["v1"]))
            any_success = True
            for pr in zara_products:
                skey = "p:" + str(pr["v1"])
                if str(pr["v1"]) in found:
                    failures.pop(skey, None)
                else:
                    failures[skey] = failures.get(skey, 0) + 1
                    log.warning("Ürün yanıtta yok (%d. kez): %s",
                                failures[skey], pr.get("name", pr["v1"]))
        except RuntimeError as exc:
            for pr in zara_products:
                skey = "p:" + str(pr["v1"])
                failures[skey] = failures.get(skey, 0) + 1
            log.error("Ürün detayları alınamadı: %s", exc)

    for pr in mango_products:
        skey = "p:" + pr["key"]
        try:
            key, entry = fetch_mango_product(pr["url"], pr["gender_slug"],
                                             pr["garment_id"], pr["color_code"])
            current[key] = entry
            failures.pop(skey, None)
            any_success = True
        except RuntimeError as exc:
            failures[skey] = failures.get(skey, 0) + 1
            log.error("Mango ürünü okunamadı (%d. kez): %s",
                      failures[skey], pr.get("name", pr["garment_id"]))
        time.sleep(random.uniform(3, 6))

    return current, any_success


def warn_dead_sources(env, state, watchlist, dry_run):
    """5 tur üst üste okunamayan kaynak için bir kez uyarı gönderir."""
    failures = state.get("source_failures", {})
    for skey, count in failures.items():
        if count != MAX_CONSECUTIVE_FAILURES:
            continue
        if skey.startswith("p:"):
            name = next((p.get("name", skey) for p in watchlist["products"]
                         if str(p.get("v1", p.get("key"))) == skey[2:]), skey)
        else:
            name = "Favori listesi: " + skey[3:]
        text = (f"⚠️ {name} {count} turdur okunamıyor. Link ölmüş veya ürün "
                "kaldırılmış olabilir. Takipten çıkarmak için /liste ve /sil.")
        log.warning(text)
        if not dry_run:
            try:
                send_telegram(env, text, disable_preview=True)
            except RuntimeError as exc:
                log.error("Uyarı gönderilemedi: %s", exc)


def run_check(config, env, dry_run=False):
    state = load_state()
    watchlist = load_watchlist(config)
    first_run = "products" not in state
    previous = dict(state.get("products", {}))

    # 1) Gruptan gelen yeni linkleri / komutları işle
    if not dry_run:
        try:
            if poll_group_messages(env, config, state, watchlist):
                save_watchlist(watchlist)
        except Exception:
            log.exception("Grup mesajları işlenirken hata — tur devam ediyor")

    # 2) Tüm kaynaklardan güncel durumu topla
    current, any_success = build_snapshot(state, watchlist)
    if not any_success:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        log.error("Hiçbir kaynak okunamadı (%d üst üste)", state["consecutive_failures"])
        if state["consecutive_failures"] == MAX_CONSECUTIVE_FAILURES and not dry_run:
            try:
                send_telegram(env, "⚠️ Zara stok takibi: hiçbir kaynak "
                                   f"{state['consecutive_failures']} turdur okunamıyor. "
                                   "Zara engellemiş olabilir.", disable_preview=True)
            except RuntimeError as exc:
                log.error("Uyarı mesajı da gönderilemedi: %s", exc)
        save_state(state)
        return 1
    state["consecutive_failures"] = 0
    warn_dead_sources(env, state, watchlist, dry_run)

    # 3) Karşılaştır ve bildir
    if first_run:
        log.info("İlk çalıştırma: %d ürün kaydedildi, bildirim atlanıyor", len(current))
    else:
        restocks = find_restocks(config, previous, current)
        if restocks:
            for entry, sizes in restocks:
                log.info("STOKTA: %s [%s] %s", entry["name"], ", ".join(sizes), entry["url"])
                if dry_run:
                    print(format_message(entry, sizes))
                    print(f"[resim: {entry.get('image', '') or '-'}]\n")
                else:
                    send_telegram(env, format_message(entry, sizes),
                                  photo=entry.get("image") or None)
                    time.sleep(random.uniform(1, 2))
        else:
            log.info("Değişiklik yok (%d ürün kontrol edildi)", len(current))

        for key, entry in current.items():
            avail = [s for s, a in entry["sizes"].items() if a in AVAILABLE_DEFAULT]
            log.debug("%s | %s | stokta: %s", entry["ref"], entry["name"],
                      ", ".join(avail) or "-")

    # 4) Kaydet — okunamayan kaynakların eski kayıtları korunur
    merged = dict(previous)
    merged.update(current)
    tracked_keys = set(current)
    if any(state.get("source_failures", {}).get("wl:" + w["url"].split("?")[0])
           for w in watchlist["wishlists"]):
        tracked_keys |= set(previous)  # wishlist okunamadıysa eskileri silme
    state["products"] = {k: v for k, v in merged.items() if k in tracked_keys}
    state["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Zara stok takip botu")
    parser.add_argument("--test", action="store_true",
                        help="Gruba örnek bildirim gönder ve çık")
    parser.add_argument("--once", action="store_true",
                        help="Tek tur çalış (varsayılan davranış)")
    parser.add_argument("--loop", action="store_true",
                        help="Sürekli çalış, CHECK_INTERVAL_MIN dakikada bir kontrol et")
    parser.add_argument("--dry-run", action="store_true",
                        help="Kontrol et ama Telegram'a mesaj gönderme")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Ayrıntılı log (beden bazında durum dökümü)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    env = load_env()
    config = load_config()

    if args.test:
        sample = {
            "name": "ÖRNEK ÜRÜN — KURULUM TESTİ", "color": "Kırmızı",
            "price": 79000, "old_price": 109000, "discount": 27,
            "url": config.get("wishlist_url", "https://www.zara.com/tr/tr/"),
        }
        products = load_state().get("products", {})
        with_image = next((p for p in products.values() if p.get("image")), None)
        sizes = ["S", "M"]
        if with_image:
            sample = {**with_image, "name": f"KURULUM TESTİ — {with_image['name']}"}
            sizes = list(with_image.get("sizes", {})) or sizes
        text = format_message(sample, sizes) + (
            "\n\n⚠️ Bu bir kurulum testidir, gerçek stok bilgisi değildir."
        )
        send_telegram(env, text, photo=sample.get("image") or None)
        log.info("Test mesajı gönderildi ✔")
        return 0

    if args.loop:
        interval = max(15, int(env.get("CHECK_INTERVAL_MIN", "20"))) * 60
        log.info("Döngü modu: stok kontrolü her %d dk, komutlar her 60 sn",
                 interval // 60)
        last_check = 0.0
        while True:
            try:
                if time.time() - last_check >= interval * random.uniform(0.97, 1.03):
                    run_check(config, env, dry_run=args.dry_run)
                    last_check = time.time()
                elif not args.dry_run:
                    # Ara turlarda sadece grup komutlarını işle → anlık yanıt
                    state = load_state()
                    watchlist = load_watchlist(config)
                    if poll_group_messages(env, config, state, watchlist):
                        save_watchlist(watchlist)
                    save_state(state)
            except Exception:
                log.exception("Beklenmeyen hata — döngü devam ediyor")
            time.sleep(60)
    else:
        return run_check(config, env, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main() or 0)
