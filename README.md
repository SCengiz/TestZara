# Zara Stok Takip + Telegram Bildirim Botu

Zara'da paylaşılan (public) favori listenizi periyodik kontrol eder; tükenmiş
veya "coming soon" olan bir ürünün bir bedeni stoğa girdiğinde Telegram'dan
bildirim gönderir.

## Nasıl çalışır?

Zara'nın "listeyi paylaş" linki herkese açıktır ve sayfanın HTML'i, listedeki
**her ürünün beden bazında stok durumunu** hazır içerir. Bot her turda bu tek
sayfayı indirir (ürün başına ayrı istek atmaz), önceki durumla karşılaştırır ve
sadece `tükendi/coming soon → stokta` **geçişlerinde** mesaj atar. Zaten
stokta olan ürünler için tekrar tekrar mesaj gelmez.

```
cron / systemd timer (20 dk'da bir)
        │
        ▼
  checker.py ──► Zara paylaşılan wishlist sayfası (tek istek)
        │
        ├─► state.json   (önceki stok durumu)
        └─► Telegram Bot API ──► size mesaj
```

## Kurulum

### 1. Telegram botu oluşturun (bir kere)

1. Telegram'da **@BotFather**'a yazın → `/newbot`
2. Bota bir isim ve kullanıcı adı verin (kullanıcı adı `_bot` ile bitmeli)
3. BotFather'ın verdiği token'ı not edin → `TELEGRAM_BOT_TOKEN`
4. Oluşan botu açıp **Start**'a basın (bunu yapmazsanız bot size mesaj atamaz)
5. Tarayıcıda şu adresi açın (token'ı kendi token'ınızla değiştirin):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Dönen JSON'da `"chat":{"id":123456789}` değeri → `TELEGRAM_CHAT_ID`

### 2. .env dosyasını doldurun

```bash
cp .env.example .env
# .env dosyasını açıp token ve chat id'yi girin
```

### 3. Test edin

```bash
python3 checker.py --test     # Telegram'a örnek bildirim atar
python3 checker.py            # ilk tur: durumu kaydeder, bildirim atmaz
python3 checker.py            # sonraki turlar: değişiklikleri bildirir
```

### 4. Zamanlayın

**Seçenek A — cron:**
```bash
crontab -e
```
```
*/20 * * * * cd /home/sc/Workspaces/TestZara && /usr/bin/python3 checker.py >/dev/null 2>&1
```

**Seçenek B — systemd timer (önerilen):**
```bash
sudo cp deploy/zara-watcher.service deploy/zara-watcher.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zara-watcher.timer
systemctl list-timers zara-watcher.timer   # kontrol
```

**Seçenek C — GitHub Actions (7/24 açık makineniz yoksa):**
1. Bu klasörü **private** bir GitHub reposuna push'layın
2. Repo → Settings → Secrets and variables → Actions altına
   `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` ekleyin
3. `.github/workflows/check.yml` hazır — 30 dk'da bir otomatik çalışır
   (state.json her turda commit'lenerek korunur)

## Ayarlar — `config.json`

| Alan | Açıklama |
|---|---|
| `wishlist_url` | Takip edilecek paylaşılan favori listesi linki |
| `notify_low_on_stock` | `true`: "az sayıda ürün" durumu da bildirilsin (varsayılan) |
| `size_rules` | Kategori bazlı beden kuralları (aşağıda) |
| `size_filters` | Ürün bazında istisna — `size_rules`'u ezer (aşağıda) |

### Kategori bazlı beden kuralları — `size_rules`

Anahtar, ürünün Zara'daki kategori adı (`familyName`, ör. `AYAKKABI`) veya tür
kodudur (`kind`, ör. `Wear` = giyim). Değer, bildirime izin verilen bedenler;
**boş liste = o kategoride tüm bedenler**. Hiçbir kurala uymayan ürünler
(parfüm, çanta gibi tek "bedenli" ürünler) her zaman bildirilir.

Mevcut ayar:

```json
"size_rules": {
  "AYAKKABI": ["36", "37"],
  "Wear": ["XXS", "XS", "S", "M", "32", "34", "36", "38"]
}
```

Giyimdeki rakamlı bedenler jean'ler içindir; Zara karşılığı 32=XXS, 34=XS,
36=S, 38=M olduğundan 32–38 aralığı izlenir.

### Sadece belirli bedenleri izlemek

Varsayılan: **tüm bedenler** izlenir. Belirli bir ürünün sadece belirli
bedenleri sizi ilgilendiriyorsa, ürünün referans kodunu (bildirim log'unda ve
Zara sayfasında görünen `1234/567` formatındaki kod) veya adının bir parçasını
anahtar yapın:

```json
"size_filters": {
  "5862/081": ["36", "38"],
  "SÜET BABET": ["38"]
}
```

### Listeye ürün ekleme / çıkarma

Zara uygulamasında favorilerinize ürün ekleyip listeyi **aynı linkle** tekrar
paylaştığınız sürece bot yeni ürünleri otomatik görür. Yeni paylaşımda link
değişirse `config.json` içindeki `wishlist_url`'i güncelleyin.
(Listeye yeni eklenen ürün için ilk turda bildirim atılmaz; sadece sonraki
stok değişimleri bildirilir.)

## Günlük kullanım

- **Log okumak:** `tail -f zara-watcher.log`
- **Durdurmak:** cron satırını silin veya `sudo systemctl disable --now zara-watcher.timer`
- **Durumu sıfırlamak:** `rm state.json` (sonraki tur "ilk tur" gibi davranır, bildirim atmaz)
- **Bildirimleri denemek:** `python3 checker.py --dry-run` mesajları Telegram'a
  atmak yerine ekrana yazar

## Notlar

- Kontrol sıklığını 15 dakikanın altına indirmeyin (IP engellenme riski).
- Zara sayfa yapısını değiştirirse bot 5 tur üst üste hata aldığında size
  Telegram'dan bir kez uyarı mesajı atar.
- Zara'nın kendi "Gelince haber ver" özelliğini de paralel açık tutabilirsiniz;
  yedek katman olarak işe yarar.
