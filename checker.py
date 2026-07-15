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
import json
import logging
import random
import re
import sys
import time
from pathlib import Path

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

PRODUCTS_DETAILS_API = "https://www.zara.com/tr/tr/products-details"
WISHLIST_RE = re.compile(r"https://www\.zara\.com/[a-z]{2}/[a-z]{2}/user/share/wishlist/[\w-]+[^\s]*")
PRODUCT_RE = re.compile(r"https://www\.zara\.com/[a-z]{2}/[a-z]{2}/[\w-]+-p(\d+)\.html\?[^\s]*?v1=(\d+)[^\s]*")

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
            "url": config["wishlist_url"],
            "label": "Kurulum listesi",
            "added_by": "kurulum",
        })
    return _load_json(WATCHLIST_FILE, fallback)


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
    is_man = (
        entry.get("section", "").upper() == "MAN"
        and "UNISEX" not in entry.get("name", "").upper()
    )
    limits = limits_cfg.get("MAN" if is_man else "WOMAN")
    if not limits:
        return True

    # "XS-S", "36/38" gibi kombine bedenlerde ilk parçaya göre karar ver
    token = re.split(r"[-/\s]", size_name.strip().upper())[0]

    if token in LETTER_ORDER:
        letter_max = str(limits.get("letter_max", "")).upper()
        if letter_max in LETTER_ORDER:
            return LETTER_ORDER.index(token) <= LETTER_ORDER.index(letter_max)
        return True

    if token.isdigit():
        number = int(token)
        if entry.get("family", "").upper() == "AYAKKABI":
            maximum = limits.get("shoe_max")
        else:
            maximum = limits.get("pants_max")
        return maximum is None or number <= int(maximum)

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
    "🤖 Zara stok takip botu\n\n"
    "Bu gruba bir Zara linki atın, takibe alayım:\n"
    "• Ürün linki (zara.com/tr/tr/...-p1234567.html?v1=...)\n"
    "• Paylaşılan favori listesi linki (zara.com/.../share/wishlist/...)\n\n"
    "Komutlar:\n"
    "/liste — takip edilenleri göster\n"
    "/sil <numara> — /liste'deki numarayla takipten çıkar"
)


def _watchlist_lines(watchlist):
    """/liste ve /sil için numaralı, sabit sıralı döküm."""
    lines = []
    refs = []  # ("wishlist", idx) | ("product", idx)
    for i, wl in enumerate(watchlist["wishlists"]):
        refs.append(("wishlists", i))
        lines.append(f"{len(refs)}. 📋 {wl.get('label', 'Favori listesi')}"
                     f" (ekleyen: {wl.get('added_by', '?')})")
    for i, pr in enumerate(watchlist["products"]):
        refs.append(("products", i))
        lines.append(f"{len(refs)}. 👕 {pr.get('name', pr['url'])}"
                     f" (ekleyen: {pr.get('added_by', '?')})")
    return lines, refs


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


def _handle_wishlist_link(env, state, watchlist, url, sender):
    clean = url.split("?")[0]
    if any(w["url"].split("?")[0] == clean for w in watchlist["wishlists"]):
        send_telegram(env, "ℹ️ Bu favori listesi zaten takipte.", disable_preview=True)
        return False
    try:
        snap = fetch_wishlist_snapshot(url)
    except RuntimeError as exc:
        log.error("Wishlist doğrulanamadı: %s", exc)
        send_telegram(env, f"⚠️ Liste okunamadı: {exc}", disable_preview=True)
        return False
    watchlist["wishlists"].append({
        "url": url, "label": f"{sender} listesi ({len(snap)} ürün)",
        "added_by": sender, "added_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    state.setdefault("products", {}).update(snap)
    send_telegram(env, f"✅ Favori listesi takibe alındı: {len(snap)} ürün "
                       f"(ekleyen: {sender}).\nStok girişlerinde haber veririm.",
                  disable_preview=True)
    log.info("Gruptan wishlist eklendi (%s): %d ürün", sender, len(snap))
    return True


def _handle_command(env, watchlist, text):
    cmd = text.split("@")[0].split() or [""]
    if cmd[0] in ("/liste", "/list"):
        lines, _ = _watchlist_lines(watchlist)
        send_telegram(env, "📌 Takip edilenler:\n" + "\n".join(lines) if lines
                      else "Takipte hiçbir şey yok. Gruba bir Zara linki atın.",
                      disable_preview=True)
        return False
    if cmd[0] == "/sil":
        lines, refs = _watchlist_lines(watchlist)
        try:
            n = int(cmd[1])
            kind, idx = refs[n - 1]
        except (IndexError, ValueError):
            send_telegram(env, "Kullanım: /sil <numara> — numaralar için /liste",
                          disable_preview=True)
            return False
        removed = watchlist[kind].pop(idx)
        name = removed.get("name") or removed.get("label") or removed.get("url")
        send_telegram(env, f"🗑 Takipten çıkarıldı: {name}", disable_preview=True)
        return True
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
    for update in result:
        state["tg_offset"] = max(int(state.get("tg_offset", 0)), update["update_id"])
        msg = update.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != group_id:
            continue  # sadece takip grubundan girdi al
        text = msg.get("text") or ""
        sender = (msg.get("from") or {}).get("first_name", "?")

        if text.startswith("/"):
            changed |= _handle_command(env, watchlist, text)
            continue
        for url in WISHLIST_RE.findall(text):
            changed |= _handle_wishlist_link(env, state, watchlist, url, sender)
        product_matches = list(PRODUCT_RE.finditer(text))
        for match in product_matches:
            changed |= _handle_product_link(env, config, state, watchlist,
                                            match.group(2), sender)
        # v1 parametresi olmayan ürün linki: takip edilemez, kullanıcıyı bilgilendir
        if not product_matches and not WISHLIST_RE.search(text) \
                and re.search(r"zara\.com/[^\s]*-p\d+\.html", text):
            send_telegram(env, "⚠️ Bu ürün linkinde renk bilgisi (v1=...) yok, "
                               "takip edemiyorum. Linki Zara uygulaması veya "
                               "sitesindeki Paylaş düğmesiyle kopyalayıp atın.",
                          disable_preview=True)
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

    v1_ids = [p["v1"] for p in watchlist["products"]]
    if v1_ids:
        try:
            raw_list = fetch_products_details(v1_ids)
            found = set()
            for raw in raw_list:
                for pr in watchlist["products"]:
                    snap = snapshot_from_product(raw, wanted_v1=pr["v1"])
                    for key, entry in snap.items():
                        if str(entry["url"].split("v1=")[-1]) == str(pr["v1"]):
                            current[key] = entry
                            found.add(str(pr["v1"]))
            any_success = True
            for pr in watchlist["products"]:
                skey = "p:" + str(pr["v1"])
                if str(pr["v1"]) in found:
                    failures.pop(skey, None)
                else:
                    failures[skey] = failures.get(skey, 0) + 1
                    log.warning("Ürün yanıtta yok (%d. kez): %s",
                                failures[skey], pr.get("name", pr["v1"]))
        except RuntimeError as exc:
            for pr in watchlist["products"]:
                skey = "p:" + str(pr["v1"])
                failures[skey] = failures.get(skey, 0) + 1
            log.error("Ürün detayları alınamadı: %s", exc)

    return current, any_success


def warn_dead_sources(env, state, watchlist, dry_run):
    """5 tur üst üste okunamayan kaynak için bir kez uyarı gönderir."""
    failures = state.get("source_failures", {})
    for skey, count in failures.items():
        if count != MAX_CONSECUTIVE_FAILURES:
            continue
        if skey.startswith("p:"):
            name = next((p.get("name", skey) for p in watchlist["products"]
                         if str(p["v1"]) == skey[2:]), skey)
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
        interval = max(15, int(env.get("CHECK_INTERVAL_MIN", "20")))
        log.info("Döngü modu: her %d dakikada bir kontrol", interval)
        while True:
            try:
                run_check(config, env, dry_run=args.dry_run)
            except Exception:
                log.exception("Beklenmeyen hata — döngü devam ediyor")
            time.sleep(interval * 60 * random.uniform(0.9, 1.1))
    else:
        return run_check(config, env, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main() or 0)
