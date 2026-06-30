import os
import json
import logging
import base64
from datetime import datetime
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from groq import Groq

# ============================================================
# KONFIGURASI
# ============================================================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]          # AI gratis (Groq)
SHEET_ID = os.environ["SHEET_ID"]                   # ID Google Sheet
SHEET_NAME = os.environ.get("SHEET_NAME", "Transaksi")
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "credentials.json")

# Mode jalan: "webhook" (untuk hosting seperti Render) atau "polling" (lokal)
RUN_MODE = os.environ.get("RUN_MODE", "polling").lower()
# URL publik service (wajib untuk webhook), contoh: https://namabot.onrender.com
# Di Render bisa pakai RENDER_EXTERNAL_URL yang otomatis tersedia.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
# Port yang didengarkan (Render menyediakan PORT otomatis)
PORT = int(os.environ.get("PORT", "10000"))

# Model AI gratis dari Groq
TEXT_MODEL = "llama-3.3-70b-versatile"
VISION_MODEL = "llama-3.2-90b-vision-preview"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)

# ============================================================
# GOOGLE SHEETS
# ============================================================
def get_worksheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    # Prioritas: kredensial dari environment variable (untuk hosting),
    # fallback ke file credentials.json (untuk lokal).
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=10)
        ws.append_row(
            ["Tanggal", "Jenis", "Keterangan", "Qty", "Harga Satuan", "Total", "Sumber"]
        )
    return ws


def insert_row(data: dict):
    ws = get_worksheet()
    ws.append_row(
        [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            data.get("jenis", ""),
            data.get("keterangan", ""),
            data.get("qty", ""),
            data.get("harga_satuan", ""),
            data.get("total", ""),
            data.get("sumber", "telegram"),
        ],
        value_input_option="USER_ENTERED",
    )


def _to_float(x):
    try:
        return float(str(x).replace(".", "").replace(",", ".")) if x not in ("", None) else 0.0
    except Exception:
        return 0.0


def get_all_records():
    """Ambil semua baris transaksi sebagai list of dict."""
    ws = get_worksheet()
    return ws.get_all_records()  # pakai header baris pertama


def build_report(period: str):
    """period: 'hari' (hari ini), 'bulan' (bulan ini), 'semua'."""
    records = get_all_records()
    now = datetime.now()

    masuk = keluar = modal = 0.0
    rincian = defaultdict(float)
    count = 0

    for r in records:
        tgl_str = str(r.get("Tanggal", ""))
        try:
            tgl = datetime.strptime(tgl_str[:10], "%Y-%m-%d")
        except Exception:
            continue

        if period == "hari" and tgl.date() != now.date():
            continue
        if period == "bulan" and (tgl.year != now.year or tgl.month != now.month):
            continue

        jenis = str(r.get("Jenis", "")).lower()
        total = _to_float(r.get("Total", 0))
        count += 1

        if jenis == "masuk":
            masuk += total
        elif jenis == "keluar":
            keluar += total
        elif jenis == "modal":
            modal += total

    laba = masuk - keluar
    saldo = masuk + modal - keluar
    return {
        "masuk": masuk, "keluar": keluar, "modal": modal,
        "laba": laba, "saldo": saldo, "count": count,
    }


def rupiah(n):
    return "Rp " + f"{int(round(n)):,}".replace(",", ".")


def format_report(judul: str, d: dict) -> str:
    return (
        f"📊 *{judul}*\n"
        f"────────────────\n"
        f"🟢 Pemasukan   : {rupiah(d['masuk'])}\n"
        f"🔴 Pengeluaran : {rupiah(d['keluar'])}\n"
        f"🔵 Modal: {rupiah(d['modal'])}\n"
        f"────────────────\n"
        f"💹 Laba (masuk−keluar): {rupiah(d['laba'])}\n"
        f"💼 Saldo (modal+masuk−keluar): {rupiah(d['saldo'])}\n"
        f"📝 Jumlah transaksi: {d['count']}"
    )


# ============================================================
# AI - DETEKSI TEKS
# ============================================================
SYSTEM_PROMPT = """Kamu asisten pencatat keuangan UMKM. Ekstrak transaksi dari teks user.

Klasifikasikan "jenis" menjadi salah satu dari:
- "masuk"      = pemasukan/penjualan
- "keluar"     = pengeluaran/pembelian/biaya
- "modal" = penambahan modal usaha

Aturan:
- Jika ada qty dan harga satuan, total = qty * harga_satuan.
- Jika hanya ada satu angka besar, anggap itu total (qty=1).
- Kata seperti "beli", "bayar", "biaya", "belanja" => keluar.
- Kata seperti "jual", "masuk", "laku", "terjual" => masuk.
- Kata seperti "modal", "suntik modal" => modal.
- Jika tidak ada kata kunci jelas dan menyebut produk + harga => masuk (penjualan).

Balas HANYA JSON valid, tanpa penjelasan, format:
{"jenis":"masuk|keluar|modal","keterangan":"...","qty":number,"harga_satuan":number,"total":number}

Contoh:
Input: "masuk kue bolu ketan 2 70000"
Output: {"jenis":"masuk","keterangan":"kue bolu ketan","qty":2,"harga_satuan":70000,"total":140000}

Input: "beli tepung 50000"
Output: {"jenis":"keluar","keterangan":"tepung","qty":1,"harga_satuan":50000,"total":50000}

Input: "modal 1000000"
Output: {"jenis":"modal","keterangan":"modal","qty":1,"harga_satuan":1000000,"total":1000000}
"""


def parse_text(text: str) -> dict:
    resp = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    data["sumber"] = "teks"
    return data


# ============================================================
# AI - DETEKSI FOTO STRUK (VISION)
# ============================================================
VISION_PROMPT = """Kamu membaca foto struk belanja/nota. Ekstrak menjadi JSON.
Struk biasanya adalah PENGELUARAN (jenis="keluar").
Ambil total akhir yang dibayar sebagai "total".
Ringkas nama toko/item utama sebagai "keterangan".

Balas HANYA JSON valid:
{"jenis":"keluar","keterangan":"...","qty":1,"harga_satuan":number,"total":number}
"""


def parse_receipt(image_b64: str) -> dict:
    resp = groq_client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ],
        temperature=0,
    )
    raw = resp.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    data["sumber"] = "struk"
    return data


# ============================================================
# HANDLERS TELEGRAM
# ============================================================
JENIS_LABEL = {
    "masuk": "🟢 Pemasukan",
    "keluar": "🔴 Pengeluaran",
    "modal": "🔵 Modal",
}


def format_reply(data: dict) -> str:
    label = JENIS_LABEL.get(data.get("jenis"), data.get("jenis"))
    return (
        f"✅ Tercatat!\n\n"
        f"{label}\n"
        f"📝 {data.get('keterangan','-')}\n"
        f"🔢 Qty: {data.get('qty','-')}\n"
        f"💵 Harga: {data.get('harga_satuan','-')}\n"
        f"💰 Total: Rp {int(float(data.get('total',0))):,}".replace(",", ".")
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Saya bot pencatat keuangan 📒\n\n"
        "Cara pakai:\n"
        "• Ketik transaksi, contoh:\n"
        "   masuk kue bolu ketan 2 70000\n"
        "   beli tepung 50000\n"
        "   modal 1000000\n"
        "• Atau kirim FOTO STRUK, saya baca otomatis.\n\n"
        "📊 Laporan:\n"
        "/laporan — rekap hari ini\n"
        "/laporan_bulan — rekap bulan ini\n"
        "/laporan_semua — rekap keseluruhan\n\n"
        "Semua tercatat ke Google Sheet."
    )


async def laporan_hari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        d = build_report("hari")
        judul = "Laporan Hari Ini — " + datetime.now().strftime("%d %b %Y")
        await update.message.reply_text(format_report(judul, d), parse_mode="Markdown")
    except Exception as e:
        logger.exception("Error laporan hari")
        await update.message.reply_text(f"⚠️ Gagal membuat laporan: {e}")


async def laporan_bulan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        d = build_report("bulan")
        judul = "Laporan Bulan Ini — " + datetime.now().strftime("%B %Y")
        await update.message.reply_text(format_report(judul, d), parse_mode="Markdown")
    except Exception as e:
        logger.exception("Error laporan bulan")
        await update.message.reply_text(f"⚠️ Gagal membuat laporan: {e}")


async def laporan_semua(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        d = build_report("semua")
        await update.message.reply_text(format_report("Laporan Keseluruhan", d), parse_mode="Markdown")
    except Exception as e:
        logger.exception("Error laporan semua")
        await update.message.reply_text(f"⚠️ Gagal membuat laporan: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await update.message.chat.send_action("typing")
    try:
        data = parse_text(text)
        insert_row(data)
        await update.message.reply_text(format_reply(data))
    except Exception as e:
        logger.exception("Error teks")
        await update.message.reply_text(f"⚠️ Gagal memproses: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        img_b64 = base64.b64encode(bytes(img_bytes)).decode("utf-8")

        data = parse_receipt(img_b64)
        insert_row(data)
        await update.message.reply_text(
            "📸 Struk terbaca!\n\n" + format_reply(data)
        )
    except Exception as e:
        logger.exception("Error foto")
        await update.message.reply_text(f"⚠️ Gagal membaca struk: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("laporan", laporan_hari))
    app.add_handler(CommandHandler("laporan_bulan", laporan_bulan))
    app.add_handler(CommandHandler("laporan_semua", laporan_semua))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if RUN_MODE == "webhook":
        if not WEBHOOK_URL:
            raise RuntimeError(
                "RUN_MODE=webhook tapi WEBHOOK_URL/RENDER_EXTERNAL_URL kosong. "
                "Set WEBHOOK_URL ke URL publik service Anda."
            )
        # Pakai token sebagai path rahasia agar endpoint tidak mudah ditebak.
        url_path = TELEGRAM_TOKEN
        full_url = f"{WEBHOOK_URL.rstrip('/')}/{url_path}"
        logger.info("Bot mode WEBHOOK di %s (port %s)", full_url, PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
            webhook_url=full_url,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Bot mode POLLING...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
