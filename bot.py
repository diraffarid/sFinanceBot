import os
import re
import json
import time
import uuid
import logging
import base64
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "credentials.json")

# Nama tab & kolom di Google Sheet (sudah disiapkan manual oleh user)
TAB_PEMASUKAN = "Pemasukan"
TAB_PENGELUARAN = "Pengeluaran"
TAB_MODAL = "Modal"
TAB_PENGATURAN = "Pengaturan"

HEADERS_PEMASUKAN = ["Tanggal", "Waktu", "Kategori", "Tipe/Produk", "Jumlah", "Harga Satuan", "Total", "Keterangan", "Bulan"]
HEADERS_PENGELUARAN = ["Tanggal", "Waktu", "Kategori", "Nominal", "Keterangan", "Bulan"]
HEADERS_MODAL = ["Tanggal", "Waktu", "Nominal", "Keterangan", "Bulan"]
HEADERS_PENGATURAN = ["Kategori Pemasukan", "Emoji", "Kategori Pengeluaran", "Emoji"]

CATEGORY_CACHE_TTL = 300  # detik

# Mode jalan: "webhook" (untuk hosting seperti Render) atau "polling" (lokal)
RUN_MODE = os.environ.get("RUN_MODE", "polling").lower()
# URL publik service (wajib untuk webhook), contoh: https://namabot.onrender.com
# Di Render bisa pakai RENDER_EXTERNAL_URL yang otomatis tersedia.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
# Port yang didengarkan (Render menyediakan PORT otomatis)
PORT = int(os.environ.get("PORT", "10000"))

# Model AI gratis dari Groq
TEXT_MODEL = "llama-3.3-70b-versatile"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)

# ============================================================
# GOOGLE SHEETS
# ============================================================
_spreadsheet_cache = None


def get_spreadsheet():
    global _spreadsheet_cache
    if _spreadsheet_cache is not None:
        return _spreadsheet_cache
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
    _spreadsheet_cache = gc.open_by_key(SHEET_ID)
    return _spreadsheet_cache


def get_worksheet(name: str, headers: list):
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=max(10, len(headers)))
        ws.append_row(headers)
    return ws


_categories_cache = {"data": None, "ts": 0}


def get_categories():
    """Baca daftar kategori Pemasukan & Pengeluaran dari tab Pengaturan (di-cache)."""
    now = time.time()
    if _categories_cache["data"] is not None and now - _categories_cache["ts"] < CATEGORY_CACHE_TTL:
        return _categories_cache["data"]

    ws = get_worksheet(TAB_PENGATURAN, HEADERS_PENGATURAN)
    rows = ws.get_all_values()[1:]
    masuk, keluar = [], []
    for row in rows:
        if len(row) > 0 and row[0].strip():
            masuk.append(row[0].strip())
        if len(row) > 2 and row[2].strip():
            keluar.append(row[2].strip())

    data = {"masuk": masuk, "keluar": keluar}
    _categories_cache["data"] = data
    _categories_cache["ts"] = now
    return data


def insert_transaction(data: dict):
    now = datetime.now()
    tanggal = now.strftime("%d/%m/%Y")
    waktu = now.strftime("%H:%M")
    bulan = now.strftime("%Y-%m")

    jenis = data.get("jenis")
    keterangan = data.get("keterangan", "")
    kategori = data.get("kategori", "")
    total = data.get("total", 0)

    if jenis == "masuk":
        ws = get_worksheet(TAB_PEMASUKAN, HEADERS_PEMASUKAN)
        row = [tanggal, waktu, kategori, keterangan, data.get("qty", ""), data.get("harga_satuan", ""), total, "", bulan]
    elif jenis == "keluar":
        ws = get_worksheet(TAB_PENGELUARAN, HEADERS_PENGELUARAN)
        row = [tanggal, waktu, kategori, total, keterangan, bulan]
    elif jenis == "modal":
        ws = get_worksheet(TAB_MODAL, HEADERS_MODAL)
        row = [tanggal, waktu, total, keterangan, bulan]
    else:
        raise ValueError(f"Jenis tidak dikenal: {jenis}")

    ws.append_row(row, value_input_option="USER_ENTERED")


def _to_float(x):
    try:
        s = re.sub(r"[^0-9.\-]", "", str(x))
        return float(s) if s not in ("", "-", ".") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_tanggal(s: str):
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y")
    except (TypeError, ValueError):
        return None


def _in_period(tgl: datetime, period: str, now: datetime) -> bool:
    if period == "hari":
        return tgl.date() == now.date()
    if period == "minggu":
        awal_minggu = (now - timedelta(days=now.weekday())).date()
        return awal_minggu <= tgl.date() <= now.date()
    if period == "bulan":
        return tgl.year == now.year and tgl.month == now.month
    return True  # 'semua'


def _sum_period(ws, total_field: str, period: str, now: datetime):
    total = 0.0
    count = 0
    for r in ws.get_all_records():
        tgl = _parse_tanggal(str(r.get("Tanggal", "")))
        if not tgl or not _in_period(tgl, period, now):
            continue
        total += _to_float(r.get(total_field, 0))
        count += 1
    return total, count


def build_report(period: str):
    """period: 'hari' (hari ini), 'minggu' (minggu ini), 'bulan' (bulan ini), 'semua'."""
    now = datetime.now()

    masuk, c1 = _sum_period(get_worksheet(TAB_PEMASUKAN, HEADERS_PEMASUKAN), "Total", period, now)
    keluar, c2 = _sum_period(get_worksheet(TAB_PENGELUARAN, HEADERS_PENGELUARAN), "Nominal", period, now)
    modal, c3 = _sum_period(get_worksheet(TAB_MODAL, HEADERS_MODAL), "Nominal", period, now)

    laba = masuk - keluar
    saldo = masuk + modal - keluar
    return {
        "masuk": masuk, "keluar": keluar, "modal": modal,
        "laba": laba, "saldo": saldo, "count": c1 + c2 + c3,
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
def build_system_prompt(categories: dict) -> str:
    kat_masuk = ", ".join(categories["masuk"]) or "Lainnya"
    kat_keluar = ", ".join(categories["keluar"]) or "Lainnya"
    return f"""Kamu asisten pencatat keuangan UMKM. Ekstrak transaksi dari teks user.

Klasifikasikan "jenis" menjadi salah satu dari:
- "masuk"      = pemasukan/penjualan
- "keluar"     = pengeluaran/pembelian/biaya
- "modal" = penambahan modal usaha

Untuk jenis "masuk", pilih "kategori" yang PALING SESUAI dari daftar ini: {kat_masuk}.
Untuk jenis "keluar", pilih "kategori" yang PALING SESUAI dari daftar ini: {kat_keluar}.
Untuk jenis "modal", set "kategori" ke string kosong "".
Jika tidak ada yang cocok persis, pilih kategori yang paling mendekati dari daftar tersebut.

Aturan:
- Jika ada qty dan harga satuan, total = qty * harga_satuan.
- Jika hanya ada satu angka besar, anggap itu total (qty=1).
- Kata seperti "beli", "bayar", "biaya", "belanja" => keluar.
- Kata seperti "jual", "masuk", "laku", "terjual" => masuk.
- Kata seperti "modal", "suntik modal" => modal.
- Jika tidak ada kata kunci jelas dan menyebut produk + harga => masuk (penjualan).

Balas HANYA JSON valid, tanpa penjelasan, format:
{{"jenis":"masuk|keluar|modal","kategori":"...","keterangan":"...","qty":number,"harga_satuan":number,"total":number}}

Contoh:
Input: "masuk kue bolu ketan 2 70000"
Output: {{"jenis":"masuk","kategori":"Kue & Pastry","keterangan":"kue bolu ketan","qty":2,"harga_satuan":70000,"total":140000}}

Input: "beli tepung 50000"
Output: {{"jenis":"keluar","kategori":"Bahan Baku","keterangan":"tepung","qty":1,"harga_satuan":50000,"total":50000}}

Input: "modal 1000000"
Output: {{"jenis":"modal","kategori":"","keterangan":"modal","qty":1,"harga_satuan":1000000,"total":1000000}}
"""


def parse_text(text: str) -> dict:
    categories = get_categories()
    resp = groq_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": build_system_prompt(categories)},
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
def build_vision_prompt(categories: dict) -> str:
    kat_keluar = ", ".join(categories["keluar"]) or "Lainnya"
    return f"""Kamu membaca foto struk belanja/nota. Ekstrak menjadi JSON.
Struk biasanya adalah PENGELUARAN (jenis="keluar").
Ambil total akhir yang dibayar sebagai "total".
Ringkas nama toko/item utama sebagai "keterangan".
Pilih "kategori" yang PALING SESUAI dari daftar ini: {kat_keluar}.

Balas HANYA JSON valid:
{{"jenis":"keluar","kategori":"...","keterangan":"...","qty":1,"harga_satuan":number,"total":number}}
"""


def parse_receipt(image_b64: str) -> dict:
    categories = get_categories()
    resp = groq_client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_vision_prompt(categories)},
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
TAB_LABEL = {
    "masuk": TAB_PEMASUKAN,
    "keluar": TAB_PENGELUARAN,
    "modal": TAB_MODAL,
}


def format_reply(data: dict, header: str) -> str:
    jenis = data.get("jenis")
    label = JENIS_LABEL.get(jenis, jenis)
    kategori = data.get("kategori", "")
    lines = [f"{header} {TAB_LABEL.get(jenis, jenis)}", "", label]
    if kategori:
        lines.append(f"🏷️ Kategori: {kategori}")
    lines.append(f"📝 {data.get('keterangan','-')}")
    if jenis == "masuk":
        lines.append(f"🔢 Qty: {data.get('qty','-')}")
        lines.append(f"💵 Harga: {data.get('harga_satuan','-')}")
    lines.append(("💰 Total: Rp {:,}".format(int(float(data.get('total', 0))))).replace(",", "."))
    return "\n".join(lines)


PENDING_TTL = 600  # detik, batas waktu konfirmasi sebelum kadaluarsa


def stash_pending(context: ContextTypes.DEFAULT_TYPE, data: dict) -> str:
    pid = uuid.uuid4().hex[:8]
    pending = context.user_data.setdefault("pending", {})
    now = time.time()
    for k in [k for k, v in pending.items() if now - v["ts"] > PENDING_TTL]:
        del pending[k]
    pending[pid] = {"data": data, "ts": now}
    return pid


async def send_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, prefix: str = ""):
    pid = stash_pending(context, data)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data=f"confirm:{pid}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"cancel:{pid}"),
    ]])
    text = prefix + format_reply(data, "❓ Konfirmasi simpan ke sheet")
    await update.message.reply_text(text, reply_markup=keyboard)


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, pid = query.data.split(":", 1)
    entry = context.user_data.get("pending", {}).pop(pid, None)
    if entry is None:
        await query.edit_message_text("⚠️ Konfirmasi sudah kadaluarsa, kirim ulang transaksinya ya.")
        return

    if action == "cancel":
        await query.edit_message_text("❌ Dibatalkan, tidak disimpan.")
        return

    data = entry["data"]
    try:
        insert_transaction(data)
        await query.edit_message_text(format_reply(data, "✅ Tersimpan ke sheet"))
    except Exception as e:
        logger.exception("Error simpan")
        await query.edit_message_text(f"⚠️ Gagal menyimpan: {e}")


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["💰 Pemasukan", "🛒 Pengeluaran"],
        ["🏦 Modal", "💼 Saldo"],
        ["📊 Lap. Hari Ini", "📅 Lap. Minggu"],
        ["🗓️ Lap. Bulan", "🧾 Panduan Struk"],
    ],
    resize_keyboard=True,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Saya bot pencatat keuangan 📒\n\n"
        "Cara pakai:\n"
        "• Ketik transaksi, contoh:\n"
        "   masuk kue bolu ketan 2 70000\n"
        "   beli tepung 50000\n"
        "   modal 1000000\n"
        "• Atau kirim FOTO STRUK, saya baca otomatis.\n"
        "• Atau pakai tombol menu di bawah.\n\n"
        "Setiap transaksi akan saya tampilkan dulu untuk dikonfirmasi "
        "(tombol ✅ Simpan / ❌ Batal) sebelum masuk ke sheet.\n\n"
        "📊 Laporan:\n"
        "/laporan — rekap hari ini\n"
        "/laporan_minggu — rekap minggu ini\n"
        "/laporan_bulan — rekap bulan ini\n"
        "/laporan_semua — rekap keseluruhan\n\n"
        "Ketik /help untuk contoh lengkap.",
        reply_markup=MAIN_KEYBOARD,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Panduan Pemakaian*\n\n"
        "🟢 *Pemasukan* — ketik \"masuk\" / nama produk + harga\n"
        "   masuk kue bolu ketan 2 70000\n"
        "   jual roti tawar 50000\n\n"
        "🔴 *Pengeluaran* — ketik \"beli\"/\"bayar\"/\"biaya\" + harga\n"
        "   beli tepung 50000\n"
        "   bayar listrik 150000\n\n"
        "🔵 *Modal* — ketik \"modal\" + nominal\n"
        "   modal 1000000\n\n"
        "📸 *Foto struk* — kirim foto nota belanja, AI baca otomatis "
        "sebagai pengeluaran.\n\n"
        "🔘 *Tombol menu* di bawah juga bisa dipakai untuk pintasan "
        "(laporan, saldo, panduan struk).\n\n"
        "Setiap transaksi akan ditampilkan dulu untuk dikonfirmasi "
        "(✅ Simpan / ❌ Batal) sebelum masuk ke sheet.\n\n"
        "📊 *Laporan*\n"
        "/laporan — rekap hari ini\n"
        "/laporan_minggu — rekap minggu ini\n"
        "/laporan_bulan — rekap bulan ini\n"
        "/laporan_semua — rekap keseluruhan",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def laporan_hari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        d = build_report("hari")
        judul = "Laporan Hari Ini — " + datetime.now().strftime("%d %b %Y")
        await update.message.reply_text(format_report(judul, d), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("Error laporan hari")
        await update.message.reply_text(f"⚠️ Gagal membuat laporan: {e}")


async def laporan_minggu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        d = build_report("minggu")
        judul = "Laporan Minggu Ini — " + datetime.now().strftime("%d %b %Y")
        await update.message.reply_text(format_report(judul, d), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("Error laporan minggu")
        await update.message.reply_text(f"⚠️ Gagal membuat laporan: {e}")


async def laporan_bulan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        d = build_report("bulan")
        judul = "Laporan Bulan Ini — " + datetime.now().strftime("%B %Y")
        await update.message.reply_text(format_report(judul, d), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("Error laporan bulan")
        await update.message.reply_text(f"⚠️ Gagal membuat laporan: {e}")


async def laporan_semua(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        d = build_report("semua")
        await update.message.reply_text(format_report("Laporan Keseluruhan", d), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.exception("Error laporan semua")
        await update.message.reply_text(f"⚠️ Gagal membuat laporan: {e}")


async def quick_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    try:
        d = build_report("semua")
        await update.message.reply_text(
            f"💼 *Saldo saat ini:* {rupiah(d['saldo'])}\n"
            f"(Modal {rupiah(d['modal'])} + Pemasukan {rupiah(d['masuk'])} − Pengeluaran {rupiah(d['keluar'])})",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception as e:
        logger.exception("Error saldo")
        await update.message.reply_text(f"⚠️ Gagal mengambil saldo: {e}")


async def quick_pemasukan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🟢 Ketik detail pemasukan, contoh:\nmasuk kue bolu ketan 2 70000",
        reply_markup=MAIN_KEYBOARD,
    )


async def quick_pengeluaran(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔴 Ketik detail pengeluaran, contoh:\nbeli tepung 50000",
        reply_markup=MAIN_KEYBOARD,
    )


async def quick_modal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔵 Ketik nominal modal, contoh:\nmodal 1000000",
        reply_markup=MAIN_KEYBOARD,
    )


async def quick_panduan_struk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 *Panduan Foto Struk*\n\n"
        "Kirim foto nota/struk belanja langsung ke chat ini. AI akan baca "
        "total & item otomatis, lalu tampil konfirmasi sebelum dicatat "
        "sebagai Pengeluaran.\n\n"
        "Tips: pastikan total belanja terlihat jelas di foto.",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


QUICK_ACTIONS = {
    "💰 Pemasukan": quick_pemasukan,
    "🛒 Pengeluaran": quick_pengeluaran,
    "🏦 Modal": quick_modal,
    "💼 Saldo": quick_saldo,
    "📊 Lap. Hari Ini": laporan_hari,
    "📅 Lap. Minggu": laporan_minggu,
    "🗓️ Lap. Bulan": laporan_bulan,
    "🧾 Panduan Struk": quick_panduan_struk,
}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    action = QUICK_ACTIONS.get(text.strip())
    if action:
        await action(update, context)
        return

    await update.message.chat.send_action("typing")
    try:
        data = parse_text(text)
        await send_confirmation(update, context, data)
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
        await send_confirmation(update, context, data, prefix="📸 Struk terbaca!\n\n")
    except Exception as e:
        logger.exception("Error foto")
        await update.message.reply_text(f"⚠️ Gagal membaca struk: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("laporan", laporan_hari))
    app.add_handler(CommandHandler("laporan_minggu", laporan_minggu))
    app.add_handler(CommandHandler("laporan_bulan", laporan_bulan))
    app.add_handler(CommandHandler("laporan_semua", laporan_semua))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_confirmation))

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
