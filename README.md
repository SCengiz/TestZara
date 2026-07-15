# Zara Stok Takip + Telegram Bildirim Botu

Telegram grubuna atılan Zara linklerini (ürün veya paylaşılan favori listesi)
takibe alır; tükenmiş veya "coming soon" olan bir ürünün bir bedeni stoğa
girdiğinde gruba fotoğraflı/fiyatlı bildirim gönderir.

## Nasıl çalışır?

Takip edilecek şeyler Telegram grubundan `/ekle` komutuyla yönetilir:

- **/ekle <ürün linki>** (`zara.com/tr/tr/...-p1234567.html`) →
  bot onaylar ve ürünü takibe alır. Linkte renk parametresi (`?v1=`) varsa o
  renk izlenir; yoksa bot ürünü referans aramasıyla bulur ve ilk rengini
  izler (birden çok renk varsa bunu belirtir).
- **/zara_liste1 <link> ... /zara_liste10 <link>** → 10 rezerve favori
  listesi yuvası. Yuva **boşsa** verilen link kaydedilir, **doluysa**
  eskisinin yerine geçer (güncelleme). Zara her paylaşımda yeni link
  ürettiği için listeyi tazelemenin yolu: aynı yuvaya yeni linki yazmak.
  Linksiz kullanım (`/zara_liste3`) yuvanın durumunu gösterir.
- **/liste** → 10 yuvanın tamamını (boşlar "boş" olarak) ve tekil
  ürünleri gösterir
- **/sil liste3** → 3. yuvayı boşaltır; **/sil 2** → 2. tekil ürünü çıkarır
- `/ekle` olmadan atılan linkler bilinçli olarak yok sayılır — grupta sohbet
  ederken paylaşılan linkler takibe girmez.

Bot her turda tüm kaynakların beden bazında stok durumunu okur (favori listesi
sayfası tek istekte, tekil ürünler toplu API isteğinde), önceki durumla
karşılaştırır ve sadece `tükendi/coming soon → stokta` **geçişlerinde** mesaj
atar. Zaten stokta olan ürünler için tekrar tekrar mesaj gelmez.

```
cron / systemd timer / GitHub Actions (15 dk'da bir)
        │
        ▼
  checker.py ──► Telegram getUpdates (gruptaki yeni linkler/komutlar)
        │              └─► watchlist.json  (takip edilen kaynaklar)
        ├──► Zara wishlist sayfaları + products-details API
        ├──► state.json  (önceki stok durumu)
        └──► Telegram Bot API ──► gruba bildirim
```

### Botun grup mesajlarını görebilmesi (bir kerelik kurulum)

Telegram botları varsayılan olarak gruptaki normal mesajları **görmez**
(privacy mode). Kapatmak için:

1. **@BotFather** → `/setprivacy` → botunuzu seçin → **Disable**
2. Botu gruptan çıkarıp **yeniden ekleyin** (Telegram bu değişikliği ancak
   yeniden eklenince uygular)

Bunu yapmazsanız `/liste` gibi komutlar çalışır ama düz mesaj olarak atılan
linkleri bot göremez.

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

**Seçenek B2 — macOS (kendi Mac'inizde):**
```bash
git clone https://github.com/SCengiz/TestZara.git ~/zara-watcher
cd ~/zara-watcher
python3 -m pip install requests
cp .env.example .env        # token ve chat id'yi girin
python3 checker.py --test   # Telegram testi
python3 checker.py --loop   # sürekli çalışır: komutlar 60 sn, stok 20 dk
```
Terminali kapatınca da çalışsın ve Mac açılınca kendiliğinden başlasın
isterseniz `deploy/com.zara-watcher.plist` dosyasının içindeki 2 yolu kendi
kullanıcı adınıza göre düzeltip:
```bash
cp deploy/com.zara-watcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zara-watcher.plist
```
Notlar:
- `--loop` modunda grup komutlarına (~1 dk içinde) **anlık yanıt** verilir;
  stok kontrolü yine 20 dk'da birdir.
- Mac **uykuya dalarsa** bot da durur; kapaklı MacBook'ta güç adaptörüne
  takılıyken "Prevent automatic sleeping" açık olmalı (System Settings →
  Battery → Options) veya Amphetamine benzeri bir uygulama kullanın.
- Aynı anda GitHub Actions da çalışıyorsa bildirimler **iki kez** gelir —
  Mac'e geçtiğinizde Actions'taki workflow'u kapatın (aşağıya bakın).

**Seçenek C — GitHub Actions (7/24 açık makineniz yoksa):**
1. Bu klasörü **private** bir GitHub reposuna push'layın
2. Repo → Settings → Secrets and variables → Actions altına
   `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` ekleyin
3. `.github/workflows/check.yml` hazır — 15 dk'da bir otomatik çalışır
   (private repoda aylık 2000 dk Actions kotasına dikkat; public repoda sınırsız)
   (state.json her turda commit'lenerek korunur)

## Ayarlar — `config.json`

| Alan | Açıklama |
|---|---|
| `wishlist_url` | Takip edilecek paylaşılan favori listesi linki |
| `notify_low_on_stock` | `true`: "az sayıda ürün" durumu da bildirilsin (varsayılan) |
| `limits` | Cinsiyete göre beden üst sınırları (aşağıda) |
| `size_filters` | Ürün bazında istisna — `limits`'i ezer (aşağıda) |

### Cinsiyete göre beden üst sınırları — `limits`

Ürün Zara'nın ERKEK bölümündeyse `MAN`, ÇOCUK bölümündeyse `KID`, diğer her
durumda (KADIN, unisex, adında "UNISEX" geçen) `WOMAN` sınırları uygulanır.
Sınırlar **aralıktır** (min–max, ikisi de dahil):

| Kategori | Kadın / Unisex | Erkek | Çocuk |
|---|---|---|---|
| Giyim — harf beden | XXS – M | S – L | dikkate alınmaz |
| Giyim — rakam beden (jean vb.) | 34 – 38 | 38 – 42 | dikkate alınmaz |
| Ayakkabı | 36 – 38 | 42 – 43 | bildirilmez |
| Yaş bedenli ürünler | — | — | sadece 13/14 yaş (164 cm) |
| Parfüm, çanta, "STANDART" | sınırsız | sınırsız | sınırsız |

```json
"limits": {
  "WOMAN": { "letter_min": "XXS", "letter_max": "M",
             "pants_min": 34, "pants_max": 38,
             "shoe_min": 36, "shoe_max": 38 },
  "MAN":   { "letter_min": "S", "letter_max": "L",
             "pants_min": 38, "pants_max": 42,
             "shoe_min": 42, "shoe_max": 43 },
  "KID":   { "only_sizes": ["13/14"] }
}
```

- `letter_min/max` — harf bedenli giyim aralığı; `pants_min/max` — rakam
  bedenli giyim (jean vb.); `shoe_min/max` — ayakkabı numarası
- `KID.only_sizes` — çocuk ürünlerinde sadece bu yaş bedenleri bildirilir
  (Zara formatı "13/14 yaş (164 cm)"; "13-14" yazımı da tanınır; çocuğun
  harf/rakam bedenli ürünleri ve ayakkabıları bildirilmez)
- Bedeni olmayan ürünler (parfüm, çanta — "STANDART") sınırsız bildirilir
- "XS-S" gibi kombine bedenlerde ilk parçaya bakılır

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
