# 🤖 Telegram Bot Pencatat Keuangan + AI

Bot Telegram untuk mencatat **pemasukan, pengeluaran, dan modal** ke Google Sheet.
AI gratis (Groq) mendeteksi otomatis jenis transaksi dari teks, dan bisa membaca **foto struk**.

## ✨ Fitur
- Ketik bebas, AI klasifikasikan: `masuk` / `keluar` / `modal`
- Contoh: `masuk kue bolu ketan 2 70000` → otomatis qty 2, harga 70.000, total 140.000
- Kirim **foto struk** → AI baca total & item, catat sebagai pengeluaran
- Semua masuk ke Google Sheet rapi

---

## 1. Buat Bot Telegram
1. Chat **@BotFather** di Telegram → `/newbot` → ikuti instruksi
2. Salin **token** yang diberikan → masukkan ke `TELEGRAM_TOKEN`

## 2. AI Gratis (Groq)
1. Daftar di https://console.groq.com (gratis)
2. Buat **API Key** → masukkan ke `GROQ_API_KEY`

## 3. Google Sheet + Service Account
1. Buat Google Sheet baru. Salin **ID** dari URL:
   `docs.google.com/spreadsheets/d/`**`ID_INI`**`/edit`
2. Buka https://console.cloud.google.com → buat Project
3. Aktifkan **Google Sheets API**
4. Menu **Credentials** → **Create Credentials** → **Service Account**
5. Di service account → tab **Keys** → **Add Key** → **JSON** → download
6. Rename file jadi `credentials.json`, taruh di folder ini
7. Buka file JSON, salin nilai `client_email` (mis. `xxx@xxx.iam.gserviceaccount.com`)
8. **Share Google Sheet** ke email itu sebagai **Editor**

## 4. Install & Jalankan
```bash
pip install -r requirements.txt

# salin .env.example jadi .env lalu isi nilainya
cp .env.example .env
nano .env

# load env lalu jalankan
export $(grep -v '^#' .env | xargs)
python bot.py
```

Bot aktif → buka chat bot Anda di Telegram → ketik `/start`.

---

## 📋 Struktur Sheet
Bot mencatat ke 3 tab terpisah sesuai jenis transaksi:

**Pemasukan**
| Tanggal | Waktu | Kategori | Tipe/Produk | Jumlah | Harga Satuan | Total | Keterangan | Bulan |
|---------|-------|----------|-------------|--------|--------------|-------|-----------|-------|

**Pengeluaran**
| Tanggal | Waktu | Kategori | Nominal | Keterangan | Bulan |
|---------|-------|----------|---------|-----------|-------|

**Modal**
| Tanggal | Waktu | Nominal | Keterangan | Bulan |
|---------|-------|---------|-----------|-------|

Kategori untuk Pemasukan/Pengeluaran dipilih otomatis oleh AI dari daftar di tab
**Pengaturan** (`Kategori Pemasukan` / `Kategori Pengeluaran`). Tab tersebut juga
yang dipakai untuk merekap di tab **Ringkasan**. Jika tab Pemasukan/Pengeluaran/Modal
belum ada, bot membuatnya otomatis dengan header di atas.

## 💬 Contoh penggunaan
| Kirim ke bot | Hasil |
|--------------|-------|
| `masuk kue bolu ketan 2 70000` | Pemasukan, total 140.000 |
| `beli tepung 50000` | Pengeluaran, total 50.000 |
| `modal 1000000` | Modal, 1.000.000 |
| 📷 foto struk | Pengeluaran sesuai total struk |

## 📊 Fitur Laporan
| Perintah | Fungsi |
|----------|--------|
| `/laporan` | Rekap hari ini |
| `/laporan_bulan` | Rekap bulan ini |
| `/laporan_semua` | Rekap keseluruhan |

Setiap laporan menampilkan total Pemasukan, Pengeluaran, Modal,
**Laba** (masuk − keluar), dan **Saldo** (modal + masuk − keluar).

---

## 🚀 Hosting Gratis 24 Jam (Bot Selalu On)

Bot ini mendukung **dua mode** lewat variable `RUN_MODE`:
- `polling` — untuk dijalankan di komputer/VPS sendiri (default)
- `webhook` — untuk hosting seperti Render (Telegram yang kirim update ke bot)

### Opsi A — Render Web Service (gratis permanen) + Webhook ✅
Render free tier hanya untuk **Web Service**, bukan Background Worker.
Karena itu bot ini dijalankan mode **webhook** sebagai web service.

**Langkah:**
1. Push folder ini ke repository GitHub Anda.
2. Buka https://render.com → daftar (login GitHub) → **New** → **Web Service**.
3. Pilih repo Anda. Render baca `render.yaml` (atau isi manual):
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance Type:** Free
4. Isi Environment Variables:
   - `RUN_MODE` = `webhook`
   - `TELEGRAM_TOKEN`
   - `GROQ_API_KEY`
   - `SHEET_ID`
   - `GOOGLE_CREDENTIALS_JSON` → tempel seluruh isi `credentials.json`
5. **Deploy**.

> `WEBHOOK_URL` tidak perlu diisi manual — Render otomatis menyediakan
> `RENDER_EXTERNAL_URL`, dan bot sudah membacanya otomatis.
> Bot juga otomatis mendaftarkan webhook ke Telegram saat start.

#### ⚠️ Soal "tidur" (cold start)
Render free web service **tidur setelah 15 menit idle** dan butuh 30-50 detik
untuk bangun. Untuk bot webhook ini artinya: **pesan pertama** setelah idle akan
telat ~30-50 detik diproses, lalu normal lagi. Untuk pemakaian pribadi UMKM ini
biasanya masih oke.

**Agar tidak pernah tidur (opsional):** daftar gratis di
[UptimeRobot](https://uptimerobot.com) → buat monitor HTTP(s) yang nge-ping
URL Render Anda tiap 5 menit. Ini menjaga service tetap bangun 24/7, gratis.

### Opsi B — Railway (trial $5, cukup berbulan-bulan)
Railway tidak punya free tier permanen, tapi memberi kredit trial yang cukup
menjalankan bot 24/7 berbulan-bulan. Di Railway, mode **polling** paling mudah:
cukup set `RUN_MODE=polling` (atau biarkan kosong) — tidak perlu webhook.
Push ke GitHub → New Project → Deploy from GitHub → isi environment variables.

### Opsi C — Komputer / VPS / Raspberry Pi sendiri (mode polling)
Benar-benar gratis kalau perangkat sudah ada. Pakai `systemd` agar auto-restart:
```ini
# /etc/systemd/system/finance-bot.service
[Unit]
Description=Telegram Finance Bot
After=network.target

[Service]
WorkingDirectory=/path/telegram-finance-bot
EnvironmentFile=/path/telegram-finance-bot/.env
ExecStart=/usr/bin/python3 bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now finance-bot
```

### Ringkasan
| Platform | Gratis? | Mode | Catatan |
|----------|---------|------|---------|
| **Render Web Service** | ✅ permanen, tanpa kartu | webhook | Tidur 15 mnt idle; pakai UptimeRobot biar selalu on |
| Railway | ⚠️ trial, lalu ~$5/bln | polling | Paling mudah, perlu kartu setelah trial habis |
| VPS / Pi sendiri | ✅ jika sudah punya | polling | Kontrol penuh, selalu on |

> Kondisi free tier sering berubah — cek halaman pricing Render/Railway saat deploy.


