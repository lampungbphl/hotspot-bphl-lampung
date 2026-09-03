@ -1,34 +1,49 @@
"""
build_dashboard_data.py
------------------------
Mengambil titik panas (hotspot) dari NASA FIRMS untuk wilayah kerja BPHL Lampung,
melakukan spatial join ke 3 layer boundary lokal (KPH, PBPH, Fungsi Kawasan Hutan),
lalu menyimpan hasilnya sebagai data/hotspots.geojson & data/stats.json.

Tidak ada dependensi ke database eksternal (Supabase dsb) - semua boundary
dibaca langsung dari file GeoJSON di folder data/.
"""

import csv
import io
import json
import os
import socket
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests
import urllib3.util.connection as urllib3_cn
from shapely.geometry import shape, Point
from shapely.prepared import prep

# ------------------------------------------------------------------
# Fix untuk error umum di GitHub Actions runner: "Network is unreachable"
# (errno 101) sering terjadi karena runner mencoba konek IPv6 ke host yang
# jalur IPv6-nya sedang tidak tersedia. Paksa semua koneksi pakai IPv4 saja.
# ------------------------------------------------------------------
def _allowed_gai_family():
    return socket.AF_INET

urllib3_cn.allowed_gai_family = _allowed_gai_family

# ------------------------------------------------------------------
# Konfigurasi
# ------------------------------------------------------------------

FIRMS_API_KEY = os.environ.get("FIRMS_API_KEY")
FIRMS_API_KEY = (os.environ.get("FIRMS_API_KEY") or "").strip()
if not FIRMS_API_KEY:
    print("ERROR: environment variable FIRMS_API_KEY belum diset", file=sys.stderr)
    sys.exit(1)
print(f"[debug] FIRMS_API_KEY terbaca, panjang: {len(FIRMS_API_KEY)} karakter "
      f"(awal: {FIRMS_API_KEY[:4]}***)")

# Bbox gabungan wilayah kerja BPHL Lampung: Lampung + Bengkulu + sebagian
# Sumsel/Jambi, mengikuti cakupan batas_kph.geojson & kawasan_fungsi_hutan.geojson
@ -57,18 +72,48 @@ DATA_DIR = os.path.join(REPO_ROOT, "data")
# Ambil data FIRMS
# ------------------------------------------------------------------

def fetch_firms_source(source: str) -> list[dict]:
def fetch_firms_source(source: str, max_retries: int = 4) -> list[dict]:
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FIRMS_API_KEY}/{source}/{BBOX}/{DAY_RANGE}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            last_error = e
            wait = 5 * attempt
            print(f"  percobaan {attempt}/{max_retries} gagal ({e.__class__.__name__}: {e}), "
                  f"tunggu {wait}s lalu coba lagi ...")
            time.sleep(wait)
    else:
        print(f"  GAGAL total mengambil {source} setelah {max_retries} percobaan: {last_error}")
        return None

    text = resp.text.strip()
    if not text or text.lower().startswith("invalid"):
        print(f"  peringatan: respons FIRMS untuk {source} kosong/invalid: {text[:200]}")

    # Debug: selalu tampilkan cuplikan respons mentah supaya kalau ada masalah
    # (key salah, kuota habis, format ditolak, dsb) langsung kelihatan di log Actions.
    print(f"  [debug] HTTP {resp.status_code}, panjang respons: {len(text)} karakter")
    print(f"  [debug] 200 karakter pertama: {text[:200]!r}")

    if not text:
        print(f"  peringatan: respons FIRMS untuk {source} kosong")
        return []

    lower = text.lower()
    if lower.startswith("invalid") or "invalid" in lower[:100] or "error" in lower[:100]:
        print(f"  peringatan: respons FIRMS untuk {source} mengindikasikan error: {text[:300]}")
        return []

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    for r in rows:
        r["_source"] = source
    if not rows:
        print(f"  peringatan: CSV untuk {source} punya header tapi 0 baris data "
              f"(header: {text.splitlines()[0] if text.splitlines() else '(kosong)'})")
    return rows


@ -83,17 +128,29 @@ def is_confidence_allowed(row: dict) -> bool:
        return False


def fetch_all_hotspots() -> list[dict]:
def fetch_all_hotspots() -> tuple[list[dict], list[str]]:
    all_rows = []
    gagal_sources = []
    for src in SOURCES:
        print(f"Mengambil data FIRMS: {src} ...")
        rows = fetch_firms_source(src)
        try:
            rows = fetch_firms_source(src)
        except Exception as e:
            print(f"  GAGAL tak terduga untuk {src}: {e}")
            gagal_sources.append(src)
            continue
        if rows is None:
            gagal_sources.append(src)
            rows = []
        print(f"  -> {len(rows)} titik mentah")
        all_rows.extend(rows)

    if gagal_sources:
        print(f"\nPeringatan: sumber berikut gagal diambil dan dilewati: {gagal_sources}")

    filtered = [r for r in all_rows if is_confidence_allowed(r)]
    print(f"Total mentah: {len(all_rows)}, setelah filter confidence medium+high: {len(filtered)}")
    return filtered
    return filtered, gagal_sources


# ------------------------------------------------------------------
@ -161,7 +218,16 @@ def main():
    print(f"  Fungsi kawasan hutan: {len(fungsi_boundaries)} poligon")

    print("\nMengambil hotspot dari NASA FIRMS ...")
    hotspots_raw = fetch_all_hotspots()
    hotspots_raw, gagal_sources = fetch_all_hotspots()

    if len(gagal_sources) == len(SOURCES):
        print(
            "\nERROR: SEMUA sumber FIRMS gagal diambil (kemungkinan gangguan jaringan "
            "sesaat di runner). Berhenti tanpa menimpa data lama supaya dashboard tidak "
            "salah menampilkan 0 hotspot. Coba lagi di run berikutnya.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nMelakukan spatial join ...")
    features = []
