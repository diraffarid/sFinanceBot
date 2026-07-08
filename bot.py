import os
import re
import json
import time
import uuid
import logging
import base64
import io
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from PIL import Image, ImageDraw, ImageFont
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
TAB_INVOICE = "Invoice"
TAB_TRANSAKSI = "Transaksi"
TAB_INFO_TOKO = "InfoToko"
TAB_HPP = "HPP"

HEADERS_PEMASUKAN = ["Tanggal", "Waktu", "Kategori", "Tipe/Produk", "Jumlah", "Harga Satuan", "Total", "Keterangan", "Bulan", "ID Transaksi", "Profit"]
HEADERS_PENGELUARAN = ["Tanggal", "Waktu", "Kategori", "Nominal", "Keterangan", "Bulan"]
HEADERS_MODAL = ["Tanggal", "Waktu", "Nominal", "Keterangan", "Bulan"]
HEADERS_PENGATURAN = ["Kategori Pemasukan", "Emoji", "Kategori Pengeluaran", "Emoji"]
HEADERS_INVOICE = ["ID Invoice", "No", "Tanggal", "Penerima", "Alamat", "No HP", "Items", "Ongkir", "Total", "Status"]
HEADERS_TRANSAKSI = ["ID Transaksi", "ID Invoice", "Tanggal", "Nama Item", "Qty", "Harga Satuan", "HPP Satuan", "Subtotal", "Profit"]
HEADERS_INFO_TOKO = ["Key", "Value"]
HEADERS_HPP = ["Nama Item", "HPP", "Harga Jual", "Diperbarui"]

HPP_CACHE_TTL = 300  # detik

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


def ensure_headers(ws, headers: list):
    """Selaraskan header sheet dengan skema terbaru tanpa merusak data lama."""
    current = ws.row_values(1)
    if current == headers:
        return
    if len(current) < len(headers) and current == headers[:len(current)]:
        # Skema lama adalah awalan dari skema baru: tambahkan kolom yang belum ada di akhir.
        missing = headers[len(current):]
        start_col = len(current) + 1
        end_col = len(current) + len(missing)
        cell_range = f"{gspread.utils.rowcol_to_a1(1, start_col)}:{gspread.utils.rowcol_to_a1(1, end_col)}"
        ws.update(cell_range, [missing])
        return
    # Header lama tidak cocok sama sekali dengan skema baru (mis. redesign kolom).
    # Aman untuk ditimpa hanya jika sheet belum punya data sama sekali.
    data_rows = ws.get_all_values()[1:]
    has_data = any(any(cell.strip() for cell in row) for row in data_rows)
    if not has_data:
        end_col = max(len(current), len(headers), 1)
        padded = headers + [""] * (end_col - len(headers))
        cell_range = f"{gspread.utils.rowcol_to_a1(1, 1)}:{gspread.utils.rowcol_to_a1(1, end_col)}"
        ws.update(cell_range, [padded])


def get_worksheet(name: str, headers: list):
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet(name)
        ensure_headers(ws, headers)
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


DEFAULT_INFO_TOKO = {
    "Nama Toko": "Toko Saya",
    "Username": "",
    "Bank": "",
    "No Rekening": "",
    "Atas Nama": "",
}


def get_info_toko() -> dict:
    """Baca info toko (nama, rekening, dst) dari tab InfoToko untuk dipakai di invoice."""
    ws = get_worksheet(TAB_INFO_TOKO, HEADERS_INFO_TOKO)
    rows = ws.get_all_values()[1:]
    info = dict(DEFAULT_INFO_TOKO)
    for row in rows:
        if row and row[0].strip():
            info[row[0].strip()] = row[1].strip() if len(row) > 1 else ""
    return info


_hpp_cache = {"data": None, "ts": 0}


def _invalidate_hpp_cache():
    _hpp_cache["data"] = None


def get_hpp_map() -> dict:
    """Baca daftar HPP & Harga Jual per nama item dari tab HPP (di-cache), key dinormalisasi lowercase.
    Tiap value: {"hpp": float, "harga_jual": float|None}."""
    now = time.time()
    if _hpp_cache["data"] is not None and now - _hpp_cache["ts"] < HPP_CACHE_TTL:
        return _hpp_cache["data"]

    ws = get_worksheet(TAB_HPP, HEADERS_HPP)
    rows = ws.get_all_values()[1:]
    m = {}
    for row in rows:
        if row and row[0].strip():
            hpp_val = _to_float(row[1]) if len(row) > 1 else 0.0
            harga_jual_val = _to_float(row[2]) if len(row) > 2 and row[2].strip() else None
            m[row[0].strip().lower()] = {"hpp": hpp_val, "harga_jual": harga_jual_val}

    _hpp_cache["data"] = m
    _hpp_cache["ts"] = now
    return m


def set_hpp_item(nama: str, hpp: float, harga_jual: float = None) -> str:
    """Tambah atau update HPP (dan opsional Harga Jual) untuk sebuah item. Return 'updated' atau 'added'."""
    ws = get_worksheet(TAB_HPP, HEADERS_HPP)
    rows = ws.get_all_values()
    nama_norm = nama.strip()
    waktu = datetime.now().strftime("%d/%m/%Y %H:%M")

    for i, row in enumerate(rows[1:], start=2):
        if row and row[0].strip().lower() == nama_norm.lower():
            existing_harga_jual = row[2] if len(row) > 2 else ""
            final_harga_jual = harga_jual if harga_jual is not None else existing_harga_jual
            ws.update(f"A{i}:D{i}", [[nama_norm, hpp, final_harga_jual, waktu]], value_input_option="USER_ENTERED")
            _invalidate_hpp_cache()
            return "updated"

    final_harga_jual = harga_jual if harga_jual is not None else ""
    ws.append_row([nama_norm, hpp, final_harga_jual, waktu], value_input_option="USER_ENTERED")
    _invalidate_hpp_cache()
    return "added"


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
        qty = data.get("qty", "")
        harga_satuan = data.get("harga_satuan", "")
        if "profit" in data:
            profit = data["profit"]
        else:
            hpp_map = get_hpp_map()
            info = hpp_map.get(str(keterangan).strip().lower())
            hpp_satuan = info["hpp"] if info else 0.0
            qty_val = _to_float(qty) or 1
            harga_val = _to_float(harga_satuan) or (_to_float(total) / qty_val if qty_val else 0.0)
            profit = (harga_val - hpp_satuan) * qty_val
        ws = get_worksheet(TAB_PEMASUKAN, HEADERS_PEMASUKAN)
        row = [tanggal, waktu, kategori, keterangan, qty, harga_satuan, total, "", bulan, data.get("id_transaksi", ""), profit]
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


def _sum_hpp_period(period: str, now: datetime) -> float:
    """Total HPP (modal barang terjual) dari transaksi Pemasukan pada periode tertentu,
    dicocokkan dari nama produk ('Tipe/Produk') terhadap tab HPP."""
    hpp_map = get_hpp_map()
    if not hpp_map:
        return 0.0
    ws = get_worksheet(TAB_PEMASUKAN, HEADERS_PEMASUKAN)
    total_hpp = 0.0
    for r in ws.get_all_records():
        tgl = _parse_tanggal(str(r.get("Tanggal", "")))
        if not tgl or not _in_period(tgl, period, now):
            continue
        nama = str(r.get("Tipe/Produk", "")).strip().lower()
        if not nama:
            continue
        info = hpp_map.get(nama)
        if info is None:
            continue
        hpp_satuan = info["hpp"]
        qty = _to_float(r.get("Jumlah", 0)) or 1
        total_hpp += qty * hpp_satuan
    return total_hpp


def build_report(period: str):
    """period: 'hari' (hari ini), 'minggu' (minggu ini), 'bulan' (bulan ini), 'semua'."""
    now = datetime.now()

    masuk, c1 = _sum_period(get_worksheet(TAB_PEMASUKAN, HEADERS_PEMASUKAN), "Total", period, now)
    keluar, c2 = _sum_period(get_worksheet(TAB_PENGELUARAN, HEADERS_PENGELUARAN), "Nominal", period, now)
    modal, c3 = _sum_period(get_worksheet(TAB_MODAL, HEADERS_MODAL), "Nominal", period, now)
    hpp = _sum_hpp_period(period, now)
    laba_bersih, _ = _sum_period(get_worksheet(TAB_PEMASUKAN, HEADERS_PEMASUKAN), "Profit", period, now)

    laba = masuk - keluar
    laba_kotor = masuk - hpp
    saldo = masuk + modal - keluar
    return {
        "masuk": masuk, "keluar": keluar, "modal": modal, "hpp": hpp,
        "laba": laba, "laba_kotor": laba_kotor, "laba_bersih": laba_bersih,
        "saldo": saldo, "count": c1 + c2 + c3,
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
        f"🏷️ HPP (modal barang terjual): {rupiah(d['hpp'])}\n"
        f"────────────────\n"
        f"💹 Laba (masuk−keluar): {rupiah(d['laba'])}\n"
        f"📈 Laba Kotor (masuk−HPP): {rupiah(d['laba_kotor'])}\n"
        f"💎 Laba Bersih (total profit di Pemasukan): {rupiah(d['laba_bersih'])}\n"
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

Untuk jenis "masuk", pilih "kategori" yang PALING SESUAI dari daftar ini: {kat_masuk} (untuk kategori keseluruhan maupun tiap item).
Untuk jenis "keluar", pilih "kategori" yang PALING SESUAI dari daftar ini: {kat_keluar} (untuk kategori keseluruhan maupun tiap item).
Untuk jenis "modal", set "kategori" ke string kosong "".
Jika tidak ada yang cocok persis, pilih kategori yang paling mendekati dari daftar tersebut.

Aturan:
- Jika ada qty dan harga satuan, total = qty * harga_satuan.
- Jika hanya ada satu angka besar, anggap itu total (qty=1).
- Kata seperti "beli", "bayar", "biaya", "belanja" => keluar.
- Kata seperti "jual", "masuk", "laku", "terjual" => masuk.
- Kata seperti "modal", "suntik modal" => modal.
- Jika tidak ada kata kunci jelas dan menyebut produk + harga => masuk (penjualan).
- User bisa menulis banyak baris/item sekaligus dalam satu pesan, contoh baris diawali "-" atau setiap baris baru berisi satu item.
  Jika ada lebih dari satu item, ekstrak SEMUA ke "items": list of
  {{"nama":"...","qty":number,"harga_satuan":number,"subtotal":number,"kategori":"..."}}
  dan "total" adalah jumlah seluruh subtotal. Jika hanya satu item/transaksi, "items" berisi satu elemen saja.

Balas HANYA JSON valid, tanpa penjelasan, format:
{{"jenis":"masuk|keluar|modal","kategori":"...","keterangan":"...","qty":number,"harga_satuan":number,"total":number,"items":[...]}}

Contoh:
Input: "masuk kue bolu ketan 2 70000"
Output: {{"jenis":"masuk","kategori":"Kue & Pastry","keterangan":"kue bolu ketan","qty":2,"harga_satuan":70000,"total":140000,"items":[{{"nama":"kue bolu ketan","qty":2,"harga_satuan":70000,"subtotal":140000,"kategori":"Kue & Pastry"}}]}}

Input: "beli tepung 50000"
Output: {{"jenis":"keluar","kategori":"Bahan Baku","keterangan":"tepung","qty":1,"harga_satuan":50000,"total":50000,"items":[{{"nama":"tepung","qty":1,"harga_satuan":50000,"subtotal":50000,"kategori":"Bahan Baku"}}]}}

Input: "modal 1000000"
Output: {{"jenis":"modal","kategori":"","keterangan":"modal","qty":1,"harga_satuan":1000000,"total":1000000,"items":[{{"nama":"modal","qty":1,"harga_satuan":1000000,"subtotal":1000000,"kategori":""}}]}}

Input: "masuk\\n- bolu ketan hitam S 2 5000\\n- fudgy brownies mix 1 30000"
Output: {{"jenis":"masuk","kategori":"Kue & Pastry","keterangan":"bolu ketan hitam S, fudgy brownies mix","qty":3,"harga_satuan":0,"total":40000,"items":[{{"nama":"bolu ketan hitam S","qty":2,"harga_satuan":5000,"subtotal":10000,"kategori":"Kue & Pastry"}},{{"nama":"fudgy brownies mix","qty":1,"harga_satuan":30000,"subtotal":30000,"kategori":"Kue & Pastry"}}]}}
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
Pilih "kategori" yang PALING SESUAI dari daftar ini: {kat_keluar} untuk kategori keseluruhan.
Juga ekstrak setiap baris barang di struk ke "items": list of
{{"nama":"...","qty":number,"harga_satuan":number,"subtotal":number,"kategori":"..."}}
(kategori tiap item pilih juga dari daftar yang sama).

Balas HANYA JSON valid:
{{"jenis":"keluar","kategori":"...","keterangan":"...","qty":1,"harga_satuan":number,"total":number,"items":[...]}}
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
# INVOICE
# ============================================================
def parse_invoice_item_line(line: str, hpp_map: dict = None):
    """Parse satu baris item. Harga bersifat opsional jika item punya 'Harga Jual' di sheet HPP.
    Return (item_dict, None) jika berhasil, atau (None, error_message) jika harga tidak diketahui."""
    hpp_map = hpp_map or {}
    line = line.strip()
    line = re.sub(r"^[-•*]\s*", "", line)

    # Format lengkap: nama qty harga
    m = re.match(r"^(.*\S)\s+(\d+)\s+([\d.,]+)$", line)
    if m:
        nama, qty, harga = m.group(1), int(m.group(2)), _to_float(m.group(3))
        return {"nama": nama, "qty": qty, "harga_satuan": harga, "subtotal": qty * harga}, None

    # Satu angka di akhir: ambigu antara "nama qty" (harga dari HPP) atau "nama harga" (qty=1).
    m2 = re.match(r"^(.*\S)\s+([\d.,]+)$", line)
    if m2:
        nama, num = m2.group(1), m2.group(2)
        info = hpp_map.get(nama.strip().lower())
        if info and info.get("harga_jual"):
            qty = int(_to_float(num)) or 1
            harga = info["harga_jual"]
            return {"nama": nama, "qty": qty, "harga_satuan": harga, "subtotal": qty * harga}, None
        harga = _to_float(num)
        return {"nama": nama, "qty": 1, "harga_satuan": harga, "subtotal": harga}, None

    # Nama saja, tanpa angka sama sekali: qty=1, harga wajib dari HPP.
    m3 = re.match(r"^(.*\S)$", line)
    if m3:
        nama = m3.group(1)
        info = hpp_map.get(nama.strip().lower())
        if info and info.get("harga_jual"):
            harga = info["harga_jual"]
            return {"nama": nama, "qty": 1, "harga_satuan": harga, "subtotal": harga}, None
        return None, (
            f"⚠️ Harga untuk \"{nama}\" belum diketahui. Tambahkan harga manual "
            f"(contoh: \"{nama} 1 50000\") atau set Harga Jual-nya dulu di menu ⚙️ Atur HPP."
        )

    return None, None


def parse_invoice_items(text: str):
    """Return (items, errors). errors berisi pesan untuk baris yang harganya tidak bisa ditentukan."""
    hpp_map = get_hpp_map()
    items = []
    errors = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        item, err = parse_invoice_item_line(line, hpp_map)
        if err:
            errors.append(err)
        elif item:
            items.append(item)
    return items, errors


def next_invoice_no(ws) -> str:
    count = len(ws.get_all_values()) - 1  # kurangi baris header
    return f"{max(count, 0) + 1:04d}"


def save_invoice_row(data: dict):
    ws = get_worksheet(TAB_INVOICE, HEADERS_INVOICE)
    id_invoice = uuid.uuid4().hex[:8]
    no = next_invoice_no(ws)
    now = datetime.now()
    row = [
        id_invoice, no, now.strftime("%d/%m/%Y"),
        data["penerima"], data.get("alamat", ""), data.get("no_hp", ""),
        json.dumps(data["items"], ensure_ascii=False),
        data["ongkir"], data["total"], "Draft",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return id_invoice, no


def find_invoice_row(id_invoice: str):
    ws = get_worksheet(TAB_INVOICE, HEADERS_INVOICE)
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):  # baris 1 = header
        if str(r.get("ID Invoice", "")) == id_invoice:
            return i, r
    return None, None


def update_invoice_status(row_idx: int, status: str):
    ws = get_worksheet(TAB_INVOICE, HEADERS_INVOICE)
    col = HEADERS_INVOICE.index("Status") + 1
    ws.update_cell(row_idx, col, status)


def next_transaksi_id() -> str:
    """ID Transaksi berupa angka auto-increment (mis. 0001, 0002, ...)."""
    ws = get_worksheet(TAB_TRANSAKSI, HEADERS_TRANSAKSI)
    max_id = 0
    for row in ws.get_all_values()[1:]:
        if row and row[0].strip().isdigit():
            max_id = max(max_id, int(row[0].strip()))
    return f"{max_id + 1:04d}"


def save_transaksi_items(id_transaksi: str, id_invoice: str, items: list) -> float:
    """Simpan detail per item ke sheet Transaksi, hitung untung = (harga jual - HPP) * qty.
    Return total profit gabungan semua item (untuk dicatat di kolom Profit Pemasukan)."""
    hpp_map = get_hpp_map()
    ws = get_worksheet(TAB_TRANSAKSI, HEADERS_TRANSAKSI)
    tanggal = datetime.now().strftime("%d/%m/%Y")

    rows = []
    total_profit = 0.0
    for it in items:
        nama = str(it.get("nama", ""))
        qty = _to_float(it.get("qty", 1)) or 1
        harga_satuan = _to_float(it.get("harga_satuan", 0))
        info = hpp_map.get(nama.strip().lower())
        hpp_satuan = info["hpp"] if info else 0.0
        subtotal = _to_float(it.get("subtotal", qty * harga_satuan))
        profit = (harga_satuan - hpp_satuan) * qty
        total_profit += profit
        rows.append([id_transaksi, id_invoice, tanggal, nama, qty, harga_satuan, hpp_satuan, subtotal, profit])

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    return total_profit


_FONT_DIR = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
_FONT_CACHE = {}


def get_font(size: int, bold: bool = False, italic: bool = False):
    key = (size, bold, italic)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    candidates = []
    if bold and italic:
        candidates = ["arialbi.ttf"]
    elif bold:
        candidates = ["arialbd.ttf"]
    elif italic:
        candidates = ["ariali.ttf"]
    else:
        candidates = ["arial.ttf"]
    font = None
    for name in candidates:
        try:
            font = ImageFont.truetype(os.path.join(_FONT_DIR, name), size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _text_w(draw, text, font):
    return draw.textbbox((0, 0), text, font=font)[2]


def generate_invoice_image(data: dict) -> io.BytesIO:
    info = get_info_toko()
    items = data["items"]

    W = 960
    margin = 40
    card_x0, card_x1 = margin, W - margin
    row_h = 60
    table_rows_h = row_h * max(len(items), 1)
    H = 980 + table_rows_h

    bg = (235, 235, 235)
    white = (255, 255, 255)
    black = (20, 20, 20)
    gray_text = (90, 90, 90)
    header_gray = (205, 205, 210)
    alt_row = (243, 240, 247)
    total_bar = (215, 215, 220)

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    f_brand = get_font(40, bold=True)
    f_handle = get_font(18)
    f_label = get_font(18)
    f_bold = get_font(22, bold=True)
    f_title = get_font(52, bold=True)
    f_small = get_font(18)
    f_table_head = get_font(19, bold=True)
    f_table_cell = get_font(19)
    f_total_label = get_font(20, bold=True)
    f_total_val = get_font(20, bold=True)
    f_grand_label = get_font(24, bold=True)
    f_grand_val = get_font(24, bold=True)
    f_sign = get_font(46, italic=True)

    y = 30
    # Header card (logo/nama toko)
    header_h = 160
    draw.rectangle([card_x0, y, card_x1, y + header_h], fill=white)
    nama_toko = info.get("Nama Toko", "Toko Saya")
    w = _text_w(draw, nama_toko, f_brand)
    draw.text(((W - w) / 2, y + 60), nama_toko, font=f_brand, fill=black)
    username = info.get("Username", "")
    if username:
        w = _text_w(draw, username, f_handle)
        draw.text(((W - w) / 2, y + 115), username, font=f_handle, fill=gray_text)
    y += header_h + 30

    # Kepada / Tagihan
    draw.text((card_x0 + 20, y), "Kepada :", font=f_label, fill=gray_text)
    y2 = y + 28
    draw.text((card_x0 + 20, y2), data["penerima"], font=f_bold, fill=black)
    y2 += 34
    if data.get("no_hp"):
        draw.text((card_x0 + 20, y2), data["no_hp"], font=f_small, fill=black)
        y2 += 26
    if data.get("alamat"):
        draw.text((card_x0 + 20, y2), data["alamat"], font=f_small, fill=black)
        y2 += 26

    title = "TAGIHAN"
    w = _text_w(draw, title, f_title)
    draw.text((card_x1 - w, y - 10), title, font=f_title, fill=black)
    no_text = f"No : {data.get('no', '-')}"
    w = _text_w(draw, no_text, f_small)
    draw.text((card_x1 - w, y + 60), no_text, font=f_small, fill=gray_text)
    tgl_text = f"Tgl : {data.get('tanggal', datetime.now().strftime('%d/%m/%Y'))}"
    w = _text_w(draw, tgl_text, f_small)
    draw.text((card_x1 - w, y + 86), tgl_text, font=f_small, fill=gray_text)

    y = max(y2, y + 120) + 30
    draw.line([(card_x0, y), (card_x1, y)], fill=(200, 200, 200), width=2)
    y += 30

    # Tabel item
    col_no = card_x0 + 20
    col_desk = card_x0 + 90
    col_jml = card_x0 + 470
    col_harga = card_x0 + 590
    col_total = card_x1 - 150

    draw.rectangle([card_x0, y, card_x1, y + row_h], fill=header_gray)
    ty = y + (row_h - 20) / 2
    draw.text((col_no, ty), "NO", font=f_table_head, fill=black)
    draw.text((col_desk, ty), "DESKRIPSI", font=f_table_head, fill=black)
    draw.text((col_jml, ty), "JUMLAH", font=f_table_head, fill=black)
    draw.text((col_harga, ty), "HARGA", font=f_table_head, fill=black)
    w = _text_w(draw, "TOTAL", f_table_head)
    draw.text((card_x1 - 20 - w, ty), "TOTAL", font=f_table_head, fill=black)
    y += row_h

    for idx, it in enumerate(items, start=1):
        row_bg = white if idx % 2 == 1 else alt_row
        draw.rectangle([card_x0, y, card_x1, y + row_h], fill=row_bg)
        ty = y + (row_h - 20) / 2
        draw.text((col_no, ty), str(idx), font=f_table_cell, fill=black)
        draw.text((col_desk, ty), str(it.get("nama", "-")), font=f_table_cell, fill=black)
        draw.text((col_jml, ty), str(it.get("qty", "-")), font=f_table_cell, fill=black)
        harga_txt = rupiah(it.get("harga_satuan", 0))
        draw.text((col_harga, ty), harga_txt, font=f_table_cell, fill=black)
        total_txt = rupiah(it.get("subtotal", 0))
        w = _text_w(draw, total_txt, f_table_cell)
        draw.text((card_x1 - 20 - w, ty), total_txt, font=f_table_cell, fill=black)
        y += row_h

    y += 30

    # Ringkasan total
    subtotal = sum(it.get("subtotal", 0) for it in items)
    ongkir = data.get("ongkir", 0)
    grand_total = data.get("total", subtotal + ongkir)

    summary_x = card_x1 - 350
    draw.text((summary_x, y), "Total", font=f_total_label, fill=black)
    val = rupiah(subtotal)
    w = _text_w(draw, val, f_total_val)
    draw.text((card_x1 - 20 - w, y), val, font=f_total_val, fill=black)
    y += 32
    draw.text((summary_x, y), "Ongkir", font=f_total_label, fill=black)
    val = rupiah(ongkir)
    w = _text_w(draw, val, f_total_val)
    draw.text((card_x1 - 20 - w, y), val, font=f_total_val, fill=black)
    y += 44

    draw.rectangle([card_x0, y, card_x1, y + 60], fill=total_bar)
    draw.text((summary_x, y + 18), "Total Keseluruhan", font=f_grand_label, fill=black)
    val = rupiah(grand_total)
    w = _text_w(draw, val, f_grand_val)
    draw.text((card_x1 - 20 - w, y + 18), val, font=f_grand_val, fill=black)
    y += 100

    # Metode pembayaran & tanda tangan
    bank = info.get("Bank", "")
    no_rek = info.get("No Rekening", "")
    atas_nama = info.get("Atas Nama", "")
    if bank or no_rek:
        draw.text((card_x0 + 20, y), "Metode Pembayaran", font=f_bold, fill=black)
        yy = y + 34
        if bank or no_rek:
            draw.text((card_x0 + 20, yy), f"Bank {bank} {no_rek}".strip(), font=f_small, fill=black)
            yy += 26
        if atas_nama:
            draw.text((card_x0 + 20, yy), f"atas nama {atas_nama}", font=f_small, fill=black)

    draw.text((card_x1 - 220, y), "Hormat Kami,", font=f_small, fill=black)
    sign_text = info.get("Nama Toko", "Toko Saya")
    w = _text_w(draw, sign_text, f_sign)
    draw.text((card_x1 - 40 - w, y + 30), sign_text, font=f_sign, fill=black)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "invoice.png"
    return buf


def format_invoice_preview(data: dict) -> str:
    lines = ["❓ *Konfirmasi Invoice*", "", f"👤 {data['penerima']}"]
    if data.get("no_hp"):
        lines.append(f"📱 {data['no_hp']}")
    if data.get("alamat"):
        lines.append(f"📍 {data['alamat']}")
    lines.append("")
    for it in data["items"]:
        lines.append(f"• {it['nama']} x{it['qty']} — {rupiah(it['subtotal'])}")
    lines.append("")
    lines.append(f"🚚 Ongkir: {rupiah(data['ongkir'])}")
    lines.append(f"💰 *Total: {rupiah(data['total'])}*")
    return "\n".join(lines)


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


def format_items_summary(items: list, header: str) -> str:
    lines = [header, ""]
    total = 0
    for it in items:
        subtotal = int(float(it.get("subtotal", 0) or 0))
        total += subtotal
        kategori = it.get("kategori", "")
        tag = f" ({kategori})" if kategori else ""
        lines.append(("• {} — Rp {:,}{}".format(it.get("nama", "-"), subtotal, tag)).replace(",", "."))
    lines.append("")
    lines.append(("💰 Total: Rp {:,}".format(total)).replace(",", "."))
    return "\n".join(lines)


async def ask_batch_mode(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict, prefix: str = ""):
    pid = stash_pending(context, data)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🧾 Total saja", callback_data=f"batchmode:total:{pid}"),
        InlineKeyboardButton("📋 Per barang", callback_data=f"batchmode:items:{pid}"),
    ]])
    total = int(float(data.get("total", 0) or 0))
    n_items = len(data.get("items", []))
    text = (
        prefix
        + ("💰 Total: Rp {:,}".format(total)).replace(",", ".") + "\n"
        + f"🧾 Item terdeteksi: {n_items}\n\n"
        "Mau dicatat sebagai total keseluruhan atau per barang?"
    )
    await update.message.reply_text(text, reply_markup=keyboard)


async def handle_batch_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, mode, pid = query.data.split(":", 2)
    entry = context.user_data.get("pending", {}).pop(pid, None)
    if entry is None:
        await query.edit_message_text("⚠️ Konfirmasi sudah kadaluarsa, kirim ulang transaksinya ya.")
        return

    data = entry["data"]
    jenis = data.get("jenis")
    if mode == "items":
        new_data = {"jenis": f"{jenis}_multi", "items": data.get("items", [])}
        text = format_items_summary(new_data["items"], "❓ Konfirmasi simpan per barang ke sheet")
    else:
        new_data = {k: v for k, v in data.items() if k != "items"}
        text = format_reply(new_data, "❓ Konfirmasi simpan ke sheet")

    new_pid = stash_pending(context, new_data)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data=f"confirm:{new_pid}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"cancel:{new_pid}"),
    ]])
    await query.edit_message_text(text, reply_markup=keyboard)


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
        jenis = data.get("jenis", "")
        if jenis in ("keluar_multi", "masuk_multi"):
            base_jenis = jenis.split("_")[0]
            items = data.get("items", [])
            for item in items:
                row = {
                    "jenis": base_jenis,
                    "kategori": item.get("kategori", ""),
                    "keterangan": item.get("nama", ""),
                    "total": item.get("subtotal", 0),
                }
                if base_jenis == "masuk":
                    row["qty"] = item.get("qty", "")
                    row["harga_satuan"] = item.get("harga_satuan", "")
                insert_transaction(row)
            await query.edit_message_text(format_items_summary(items, "✅ Tersimpan ke sheet"))
        else:
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
        ["📄 Cek Laporan", "🧾 Buat Invoice"],
        ["➕ Input Transaksi", "⚙️ Atur HPP"],
    ],
    resize_keyboard=True,
)

LAPORAN_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1-K0d8-k-1VFrmQtCXK6YLGM13S427mbmjJMmKKu6ask/edit?gid=1315232313#gid=1315232313"


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
        "🟢 Ketik detail pemasukan, contoh:\nmasuk kue bolu ketan 2 70000\n\n"
        "Bisa juga banyak item sekaligus:\n"
        "masuk\n- bolu ketan hitam S 2 5000\n- fudgy brownies mix 1 30000",
        reply_markup=MAIN_KEYBOARD,
    )


async def quick_pengeluaran(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔴 Ketik detail pengeluaran, contoh:\nbeli tepung 50000\n\n"
        "Bisa juga banyak item sekaligus:\n"
        "beli\n- tepung 50000\n- gula 20000",
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


async def quick_cek_laporan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📄 Cek laporan lengkap di spreadsheet berikut:\n{LAPORAN_SPREADSHEET_URL}",
        reply_markup=MAIN_KEYBOARD,
        disable_web_page_preview=True,
    )


async def start_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["invoice_flow"] = {"step": "penerima", "data": {}}
    await update.message.reply_text(
        "🧾 *Buat Invoice Baru*\n\nSiapa nama penerima? (ketik /batal untuk membatalkan)",
        parse_mode="Markdown",
    )


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancelled = (
        context.user_data.pop("invoice_flow", None)
        or context.user_data.pop("transaksi_flow", None)
        or context.user_data.pop("hpp_flow", None)
    )
    if cancelled:
        await update.message.reply_text("❌ Dibatalkan.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text("Tidak ada proses yang sedang berjalan.", reply_markup=MAIN_KEYBOARD)


async def handle_invoice_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict):
    text = update.message.text.strip()
    if text == "/batal":
        context.user_data.pop("invoice_flow", None)
        await update.message.reply_text("❌ Invoice dibatalkan.", reply_markup=MAIN_KEYBOARD)
        return

    step = flow["step"]
    data = flow["data"]

    if step == "penerima":
        data["penerima"] = text
        flow["step"] = "alamat"
        await update.message.reply_text("📍 Alamat penerima? (ketik - jika tidak ada)")
    elif step == "alamat":
        data["alamat"] = "" if text == "-" else text
        flow["step"] = "no_hp"
        await update.message.reply_text("📱 No HP penerima? (ketik - jika tidak ada)")
    elif step == "no_hp":
        data["no_hp"] = "" if text == "-" else text
        flow["step"] = "items"
        await update.message.reply_text(
            "🧾 Ketik daftar item, satu baris per item.\n"
            "Format: nama qty harga (harga boleh dikosongkan jika sudah diatur di ⚙️ Atur HPP)\n\n"
            "Contoh:\nPaket Topping Matcha 1 65000\nBolu Ketan Hitam S 2\n(baris kedua otomatis pakai Harga Jual dari HPP)"
        )
    elif step == "items":
        items, errors = parse_invoice_items(text)
        if errors:
            await update.message.reply_text("\n".join(errors))
            return
        if not items:
            await update.message.reply_text(
                "⚠️ Format tidak dikenali, coba lagi.\nContoh: Paket Topping Matcha 1 65000"
            )
            return
        data["items"] = items
        flow["step"] = "ongkir"
        await update.message.reply_text("🚚 Ongkir berapa? (ketik 0 jika tidak ada)")
    elif step == "ongkir":
        ongkir = _to_float(text)
        data["ongkir"] = ongkir
        subtotal = sum(it["subtotal"] for it in data["items"])
        data["total"] = subtotal + ongkir
        context.user_data.pop("invoice_flow", None)

        pid = stash_pending(context, {"jenis": "invoice", **data})
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Buat Invoice", callback_data=f"invoicemake:{pid}"),
            InlineKeyboardButton("❌ Batal", callback_data=f"invoicecancel:{pid}"),
        ]])
        await update.message.reply_text(
            format_invoice_preview(data), reply_markup=keyboard, parse_mode="Markdown"
        )


async def handle_invoice_make(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pid = query.data.split(":", 1)
    entry = context.user_data.get("pending", {}).pop(pid, None)
    if entry is None:
        await query.edit_message_text("⚠️ Konfirmasi sudah kadaluarsa, mulai ulang /invoice.")
        return

    data = entry["data"]
    try:
        id_invoice, no = save_invoice_row(data)
        data["id_invoice"] = id_invoice
        data["no"] = no
        buf = generate_invoice_image(data)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Jadikan Transaksi", callback_data=f"invoice2trx:{id_invoice}"),
        ]])
        await query.message.reply_photo(
            photo=buf,
            caption=f"🧾 Invoice No. {no} berhasil dibuat untuk {data['penerima']}.",
            reply_markup=keyboard,
        )
        await query.edit_message_text(f"✅ Invoice No. {no} dibuat. Lihat foto di bawah.")
    except Exception as e:
        logger.exception("Error buat invoice")
        await query.edit_message_text(f"⚠️ Gagal membuat invoice: {e}")


async def handle_invoice_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pid = query.data.split(":", 1)
    context.user_data.get("pending", {}).pop(pid, None)
    await query.edit_message_text("❌ Invoice dibatalkan.")


async def handle_invoice_to_transaksi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, id_invoice = query.data.split(":", 1)
    try:
        row_idx, inv = find_invoice_row(id_invoice)
        if inv is None:
            await query.message.reply_text("⚠️ Invoice tidak ditemukan.")
            return
        if inv.get("Status") == "Transaksi":
            await query.message.reply_text("⚠️ Invoice ini sudah pernah dijadikan transaksi.")
            return

        id_transaksi = next_transaksi_id()
        total = _to_float(inv.get("Total", 0))
        penerima = inv.get("Penerima", "")
        no = inv.get("No", "")
        items = json.loads(inv.get("Items", "[]") or "[]")

        total_profit = save_transaksi_items(id_transaksi, id_invoice, items)

        insert_transaction({
            "jenis": "masuk",
            "kategori": "",
            "keterangan": f"Invoice {no} - {penerima}",
            "total": total,
            "id_transaksi": id_transaksi,
            "profit": total_profit,
        })

        update_invoice_status(row_idx, "Transaksi")

        caption = (query.message.caption or "") + f"\n\n✅ Dicatat sebagai Pemasukan (ID Transaksi: {id_transaksi})."
        await query.edit_message_caption(caption=caption)
    except Exception as e:
        logger.exception("Error jadikan transaksi")
        await query.message.reply_text(f"⚠️ Gagal menjadikan transaksi: {e}")


async def start_hpp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["hpp_flow"] = {"step": "nama", "data": {}}
    await update.message.reply_text(
        "⚙️ *Atur HPP Item*\n\n"
        "Nama item yang mau ditambah/diubah HPP-nya? (ketik /batal untuk membatalkan)",
        parse_mode="Markdown",
    )


async def handle_hpp_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict):
    text = update.message.text.strip()
    if text == "/batal":
        context.user_data.pop("hpp_flow", None)
        await update.message.reply_text("❌ Dibatalkan.", reply_markup=MAIN_KEYBOARD)
        return

    step = flow["step"]
    data = flow["data"]

    if step == "nama":
        data["nama"] = text
        flow["step"] = "hpp"
        existing = get_hpp_map().get(text.strip().lower())
        hint = f"\n(HPP saat ini: {rupiah(existing['hpp'])})" if existing is not None else ""
        await update.message.reply_text(f"💰 HPP (modal) per item untuk \"{text}\"? (Rp){hint}")
    elif step == "hpp":
        data["hpp"] = _to_float(text)
        flow["step"] = "harga_jual"
        existing = get_hpp_map().get(data["nama"].strip().lower())
        hint = ""
        if existing is not None and existing.get("harga_jual") is not None:
            hint = f"\n(Harga jual saat ini: {rupiah(existing['harga_jual'])})"
        await update.message.reply_text(
            f"🏷️ Harga jual per item untuk \"{data['nama']}\"? (Rp, ketik - jika tidak ingin diatur){hint}"
        )
    elif step == "harga_jual":
        harga_jual = None if text == "-" else _to_float(text)
        nama = data["nama"]
        hpp_val = data["hpp"]
        context.user_data.pop("hpp_flow", None)
        try:
            status = set_hpp_item(nama, hpp_val, harga_jual)
            label = "diperbarui" if status == "updated" else "ditambahkan"
            msg = f"✅ HPP untuk \"{nama}\" {label}: {rupiah(hpp_val)}"
            if harga_jual is not None:
                msg += f"\n🏷️ Harga jual: {rupiah(harga_jual)}"
            await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            logger.exception("Error simpan HPP")
            await update.message.reply_text(f"⚠️ Gagal menyimpan HPP: {e}", reply_markup=MAIN_KEYBOARD)


async def start_transaksi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["transaksi_flow"] = {"step": "keterangan", "data": {}}
    await update.message.reply_text(
        "➕ *Input Transaksi Baru*\n\n"
        "Keterangan/nama pembeli transaksi ini? (ketik /batal untuk membatalkan)",
        parse_mode="Markdown",
    )


async def handle_transaksi_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict):
    text = update.message.text.strip()
    if text == "/batal":
        context.user_data.pop("transaksi_flow", None)
        await update.message.reply_text("❌ Transaksi dibatalkan.", reply_markup=MAIN_KEYBOARD)
        return

    step = flow["step"]
    data = flow["data"]

    if step == "keterangan":
        data["keterangan"] = text
        flow["step"] = "items"
        await update.message.reply_text(
            "🧾 Ketik daftar item, satu baris per item.\n"
            "Format: nama qty harga (harga boleh dikosongkan jika sudah diatur di ⚙️ Atur HPP)\n\n"
            "Contoh:\nbolu ketan hitam S 2 5000\nfudgy brownies mix 1\n(baris kedua otomatis pakai Harga Jual dari HPP)"
        )
    elif step == "items":
        items, errors = parse_invoice_items(text)
        if errors:
            await update.message.reply_text("\n".join(errors))
            return
        if not items:
            await update.message.reply_text(
                "⚠️ Format tidak dikenali, coba lagi.\nContoh: bolu ketan hitam S 2 5000"
            )
            return
        data["items"] = items
        flow["step"] = "ongkir"
        await update.message.reply_text("🚚 Ongkir berapa? (ketik 0 jika tidak ada)")
    elif step == "ongkir":
        ongkir = _to_float(text)
        data["ongkir"] = ongkir
        subtotal = sum(it["subtotal"] for it in data["items"])
        data["total"] = subtotal + ongkir
        context.user_data.pop("transaksi_flow", None)

        pid = stash_pending(context, {"jenis": "transaksi_manual", **data})
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Simpan", callback_data=f"trxmake:{pid}"),
            InlineKeyboardButton("❌ Batal", callback_data=f"trxcancel:{pid}"),
        ]])
        lines = [f"❓ *Konfirmasi Transaksi*\n\n📝 {data['keterangan']}", ""]
        for it in data["items"]:
            lines.append(f"• {it['nama']} x{it['qty']} — {rupiah(it['subtotal'])}")
        lines.append("")
        lines.append(f"🚚 Ongkir: {rupiah(ongkir)}")
        lines.append(f"💰 *Total: {rupiah(data['total'])}*")
        await update.message.reply_text("\n".join(lines), reply_markup=keyboard, parse_mode="Markdown")


async def handle_transaksi_make(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pid = query.data.split(":", 1)
    entry = context.user_data.get("pending", {}).pop(pid, None)
    if entry is None:
        await query.edit_message_text("⚠️ Konfirmasi sudah kadaluarsa, mulai ulang /transaksi.")
        return

    data = entry["data"]
    try:
        id_transaksi = next_transaksi_id()
        total_profit = save_transaksi_items(id_transaksi, "", data["items"])
        insert_transaction({
            "jenis": "masuk",
            "kategori": "",
            "keterangan": data["keterangan"],
            "total": data["total"],
            "id_transaksi": id_transaksi,
            "profit": total_profit,
        })
        text = format_items_summary(
            data["items"], f"✅ Tersimpan ke sheet (ID Transaksi: {id_transaksi})"
        )
        await query.edit_message_text(text)
    except Exception as e:
        logger.exception("Error simpan transaksi")
        await query.edit_message_text(f"⚠️ Gagal menyimpan: {e}")


async def handle_transaksi_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pid = query.data.split(":", 1)
    context.user_data.get("pending", {}).pop(pid, None)
    await query.edit_message_text("❌ Dibatalkan, tidak disimpan.")


QUICK_ACTIONS = {
    "💰 Pemasukan": quick_pemasukan,
    "🛒 Pengeluaran": quick_pengeluaran,
    "🏦 Modal": quick_modal,
    "💼 Saldo": quick_saldo,
    "📊 Lap. Hari Ini": laporan_hari,
    "📅 Lap. Minggu": laporan_minggu,
    "🗓️ Lap. Bulan": laporan_bulan,
    "🧾 Panduan Struk": quick_panduan_struk,
    "📄 Cek Laporan": quick_cek_laporan,
    "🧾 Buat Invoice": start_invoice,
    "➕ Input Transaksi": start_transaksi,
    "⚙️ Atur HPP": start_hpp,
}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    flow = context.user_data.get("invoice_flow")
    if flow:
        await handle_invoice_flow(update, context, flow)
        return

    flow = context.user_data.get("transaksi_flow")
    if flow:
        await handle_transaksi_flow(update, context, flow)
        return

    flow = context.user_data.get("hpp_flow")
    if flow:
        await handle_hpp_flow(update, context, flow)
        return

    action = QUICK_ACTIONS.get(text.strip())
    if action:
        await action(update, context)
        return

    await update.message.chat.send_action("typing")
    try:
        data = parse_text(text)
        items = data.get("items") or []
        if data.get("jenis") in ("masuk", "keluar") and len(items) > 1:
            await ask_batch_mode(update, context, data, prefix="📝 Terdeteksi banyak item!\n\n")
        else:
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
        items = data.get("items") or []
        if len(items) > 1:
            await ask_batch_mode(update, context, data, prefix="📸 Struk terbaca!\n\n")
        else:
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
    app.add_handler(CommandHandler("invoice", start_invoice))
    app.add_handler(CommandHandler("transaksi", start_transaksi))
    app.add_handler(CommandHandler("hpp", start_hpp))
    app.add_handler(CommandHandler("batal", cancel_flow))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_batch_mode, pattern=r"^batchmode:"))
    app.add_handler(CallbackQueryHandler(handle_invoice_make, pattern=r"^invoicemake:"))
    app.add_handler(CallbackQueryHandler(handle_invoice_cancel, pattern=r"^invoicecancel:"))
    app.add_handler(CallbackQueryHandler(handle_invoice_to_transaksi, pattern=r"^invoice2trx:"))
    app.add_handler(CallbackQueryHandler(handle_transaksi_make, pattern=r"^trxmake:"))
    app.add_handler(CallbackQueryHandler(handle_transaksi_cancel, pattern=r"^trxcancel:"))
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
