#!/usr/bin/env python3
"""Zara favori listesi stok takibi + Telegram bildirimi.

Paylaşılan (public) Zara wishlist sayfasını çeker, HTML içine gömülü
`window.zara.viewPayload` JSON'undan her ürünün beden bazında stok durumunu
okur, önceki durumla (state.json) karşılaştırır ve stoğa yeni giren
ürün/bedenler için Telegram'dan bildirim gönderir.

Kullanım:
    python3 checker.py            # tek tur kontrol (cron / systemd timer için)
    python3 checker.py --loop     # sürekli çalışır, CHECK_INTERVAL_MIN aralıkla
    python3 checker.py --test     # Telegram'a örnek bildirim atar, kontrol yapmaz
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

# Bu durumlar "alınabilir" sayılır; diğer her şey (out_of_stock, coming_soon,
# back_soon vb.) "alınamaz" sayılır.
AVAILABLE_DEFAULT = {"in_stock", "low_on_stock"}

VIEW_PAYLOAD_MARKER = "window.zara.viewPayload = "
MAX_CONSECUTIVE_FAILURES = 5

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


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("state.json beklenen formatta değil")
        return state
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("state.json okunamadı (%s) — boş state ile devam", exc)
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    tmp.replace(STATE_FILE)


# ------------------------------------------------------------------- zara http

def fetch_wishlist_html(url):
    """Wishlist sayfasını indirir. 3 deneme, 2/4/8 sn backoff."""
    last_exc = None
    for attempt, delay in enumerate((0, 2, 4), start=1):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code in (403, 429):
                # Bot koruması / rate limit — bu turu atla, sonraki turda dene
                raise RuntimeError(f"Zara isteği engellendi (HTTP {resp.status_code})")
            resp.raise_for_status()
            return resp.text
        except (requests.RequestException, RuntimeError) as exc:
            last_exc = exc
            log.warning("İstek başarısız (deneme %d/3): %s", attempt, exc)
    raise RuntimeError(f"Wishlist sayfası alınamadı: {last_exc}")


def parse_wishlist(html):
    """HTML içindeki viewPayload JSON'unu çıkarır, ürün anlık görüntüsü döndürür.

    Dönen yapı: {product_key: {"name", "url", "color", "ref", "sizes": {beden: durum}}}
    """
    idx = html.find(VIEW_PAYLOAD_MARKER)
    if idx < 0:
        raise RuntimeError(
            "Sayfada viewPayload bulunamadı — Zara sayfa yapısını değiştirmiş olabilir"
        )
    payload, _ = json.JSONDecoder().raw_decode(html, idx + len(VIEW_PAYLOAD_MARKER))
    wishlist = payload.get("wishlist") or {}
    items = wishlist.get("items") or []
    if not items:
        raise RuntimeError("Wishlist boş döndü — link geçersiz olabilir")

    snapshot = {}
    for item in items:
        product = item.get("product") or {}
        raw = item.get("unprocessedProduct") or {}
        seo = raw.get("seo") or {}
        colors = (raw.get("detail") or {}).get("colors") or []
        wanted_color = (product.get("color") or {}).get("id")
        color = next((c for c in colors if c.get("id") == wanted_color), None) \
            or (colors[0] if colors else {})

        seo_pid = seo.get("seoProductId") or str(raw.get("id", ""))
        color_id = color.get("id", "")
        key = f"{seo_pid}-{color_id}"

        keyword = seo.get("keyword", "urun")
        v1 = seo.get("discernProductId") or color.get("productId") or ""
        url = f"https://www.zara.com/tr/tr/{keyword}-p{seo_pid}.html?v1={v1}"

        # Ürün görseli: xmedia URL şablonundaki {width} yer tutucusunu doldur
        image = ""
        medias = color.get("xmedia") or product.get("xmedias") or []
        if medias and medias[0].get("url"):
            image = medias[0]["url"].replace("{width}", "800")

        sizes = {}
        for size in color.get("sizes") or []:
            name = str(size.get("name", "")).strip()
            if name:
                sizes[name] = size.get("availability", "unknown")
        if not sizes:
            # Beden listesi gelmediyse ürün seviyesindeki bayraklardan türet
            if product.get("isComingSoon"):
                sizes["STANDART"] = "coming_soon"
            else:
                sizes["STANDART"] = (
                    "out_of_stock" if product.get("isOutOfStock") else "in_stock"
                )

        snapshot[key] = {
            "name": product.get("name") or raw.get("name") or "?",
            "ref": product.get("displayReference", ""),
            "color": color.get("name", ""),
            "kind": raw.get("kind", ""),
            "family": raw.get("familyName", ""),
            "price": color.get("price"),
            "old_price": color.get("oldPrice"),
            "discount": color.get("displayDiscountPercentage"),
            "image": image,
            "url": url,
            "sizes": sizes,
        }
    return snapshot


# ------------------------------------------------------------------- karşılaştırma

def size_allowed(config, entry, size_name):
    """Bir bedenin bildirime konu olup olmayacağına karar verir.

    Öncelik sırası:
    1. size_filters — ürün bazlı istisna (anahtar: display reference "5862/081"
       veya ürün adının bir parçası)
    2. size_rules — kategori bazlı kural (anahtar: familyName ör. "AYAKKABI"
       veya kind ör. "Wear"); boş liste = o kategoride tüm bedenler
    3. Hiçbir kural eşleşmezse (parfüm, çanta vb.) tüm bedenler izlenir.
    """
    filters = config.get("size_filters") or {}
    for key, sizes in filters.items():
        if key == entry["ref"] or key.lower() in entry["name"].lower():
            return not sizes or size_name in sizes

    rules = config.get("size_rules") or {}
    for key in (entry.get("family", ""), entry.get("kind", "")):
        if key and key in rules:
            allowed = rules[key]
            return not allowed or size_name in allowed
    return True


def find_restocks(config, previous, current):
    """out_of_stock/coming_soon → in_stock/low_on_stock geçişlerini bulur."""
    available = set(AVAILABLE_DEFAULT)
    if not config.get("notify_low_on_stock", True):
        available.discard("low_on_stock")

    restocks = []
    for key, entry in current.items():
        prev_entry = previous.get(key)
        if prev_entry is None:
            continue  # listeye yeni eklenen ürün: sadece kaydet, bildirme
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

def _telegram_call(env, method, body):
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token or not env.get("TELEGRAM_CHAT_ID"):
        raise RuntimeError(
            ".env içinde TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID tanımlı olmalı"
        )
    api = f"https://api.telegram.org/bot{token}/{method}"
    body = {"chat_id": env["TELEGRAM_CHAT_ID"], **body}
    last = None
    for attempt, delay in enumerate((0, 2, 4), start=1):
        if delay:
            time.sleep(delay)
        try:
            resp = requests.post(api, json=body, timeout=30)
            data = resp.json()
            if data.get("ok"):
                return True
            last = data
            # 4xx: tekrar denemek anlamsız (ör. bozuk resim URL'i)
            if 400 <= resp.status_code < 500:
                break
        except (requests.RequestException, ValueError) as exc:
            last = exc
        log.warning("Telegram %s başarısız (deneme %d/3): %s", method, attempt, last)
    return False


def send_telegram(env, text, disable_preview=False, photo=None):
    """Bildirim gönderir; resim varsa sendPhoto, yoksa/başarısızsa sendMessage."""
    if photo:
        if _telegram_call(env, "sendPhoto", {"photo": photo, "caption": text}):
            return
        log.warning("sendPhoto başarısız, düz mesaja düşülüyor")
    if not _telegram_call(
        env, "sendMessage",
        {"text": text, "disable_web_page_preview": disable_preview},
    ):
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


# ------------------------------------------------------------------------- ana

def run_check(config, env, dry_run=False):
    state = load_state()
    previous = state.get("products", {})
    first_run = "products" not in state

    try:
        html = fetch_wishlist_html(config["wishlist_url"])
        current = parse_wishlist(html)
    except RuntimeError as exc:
        failures = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = failures
        log.error("Tur başarısız (%d üst üste): %s", failures, exc)
        if failures == MAX_CONSECUTIVE_FAILURES and not dry_run:
            try:
                send_telegram(
                    env,
                    "⚠️ Zara stok takibi: wishlist sayfası "
                    f"{failures} turdur okunamıyor. Link ölmüş veya Zara "
                    "sayfa yapısını değiştirmiş olabilir.\n\n"
                    f"{config['wishlist_url']}",
                    disable_preview=True,
                )
            except RuntimeError as tg_exc:
                log.error("Uyarı mesajı da gönderilemedi: %s", tg_exc)
        save_state(state)
        return 1

    state["consecutive_failures"] = 0

    if first_run:
        log.info(
            "İlk çalıştırma: %d ürün kaydedildi, bildirim atlanıyor", len(current)
        )
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

        # Ayrıntılı durum dökümü log'a
        for key, entry in current.items():
            avail = [s for s, a in entry["sizes"].items() if a in AVAILABLE_DEFAULT]
            log.debug("%s | %s | stokta: %s", entry["ref"], entry["name"],
                      ", ".join(avail) or "-")

    state["products"] = current
    state["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    return 0


def main():
    parser = argparse.ArgumentParser(description="Zara stok takip botu")
    parser.add_argument("--test", action="store_true",
                        help="Telegram'a örnek bildirim gönder ve çık")
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
        # Gerçekçi bir test için state'teki ilk üründen resim/fiyat ödünç al
        sample = {
            "name": "ÖRNEK ÜRÜN — KURULUM TESTİ",
            "color": "Kırmızı",
            "price": 79000,
            "old_price": 109000,
            "discount": 27,
            "url": config["wishlist_url"],
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
            # ± %10 rastgele sapma: istekler tam aynı ritimde gitmesin
            time.sleep(interval * 60 * random.uniform(0.9, 1.1))
    else:
        return run_check(config, env, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main() or 0)
