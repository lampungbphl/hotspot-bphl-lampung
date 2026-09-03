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

FIRMS_API_KEY = (os.environ.get("FIRMS_API_KEY") or "").strip()
if not FIRMS_API_KEY:
    print("ERROR: environment variable FIRMS_API_KEY belum diset", file=sys.stderr)
    sys.exit(1)
print(f"[debug] FIRMS_API_KEY terbaca, panjang: {len(FIRMS_API_KEY)} karakter "
      f"(awal: {FIRMS_API_KEY[:4]}***)")

# Bbox gabungan wilayah kerja BPHL Lampung: Lampung + Bengkulu + sebagian
# Sumsel/Jambi, mengikuti cakupan batas_kph.geojson & kawasan_fungsi_hutan.geojson
# format FIRMS: west,south,east,north
BBOX = "100.9,-6.3,106.4,-2.2"

# Sumber VIIRS NRT (resolusi lebih baik dari MODIS untuk titik kecil).
# Bisa ditambah "MODIS_NRT" kalau mau ikutkan juga.
SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]

# Berapa hari ke belakang yang diambil tiap run (FIRMS NRT: maksimal 10 hari per request)
DAY_RANGE = 1

# Confidence yang ditampilkan: hanya medium (nominal) & high.
# VIIRS: confidence berupa huruf -> l (low) / n (nominal) / h (high)
# MODIS (kalau dipakai): confidence berupa angka 0-100, ambang di bawah ini
#   dianggap setara "nominal" ke atas. Silakan sesuaikan kalau perlu.
VIIRS_CONFIDENCE_ALLOWED = {"n", "h"}
MODIS_CONFIDENCE_MIN = 50

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")


# ------------------------------------------------------------------
# Ambil data FIRMS
# ------------------------------------------------------------------

def fetch_firms_source(source: str, max_retries: int = 4) -> list[dict]:
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{FIRMS_API_KEY}/{source}/{BBOX}/{DAY_RANGE}"

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


def is_confidence_allowed(row: dict) -> bool:
    conf = (row.get("confidence") or "").strip().lower()
    if conf in ("l", "n", "h"):
        return conf in VIIRS_CONFIDENCE_ALLOWED
    # kemungkinan numerik (MODIS)
    try:
        return float(conf) >= MODIS_CONFIDENCE_MIN
    except ValueError:
        return False


def fetch_all_hotspots() -> tuple[list[dict], list[str]]:
    all_rows = []
    gagal_sources = []
    for src in SOURCES:
        print(f"Mengambil data FIRMS: {src} ...")
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
    return filtered, gagal_sources


# ------------------------------------------------------------------
# Boundary loader + spatial join
# ------------------------------------------------------------------

def load_boundary(filename: str, name_field_candidates: list[str]) -> list[tuple]:
    """Kembalikan list of (nama, prepared_geom, geom) dari sebuah file GeoJSON boundary."""
    path = os.path.join(DATA_DIR, filename)
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)

    result = []
    for feat in fc["features"]:
        if feat.get("geometry") is None:
            continue
        props = feat.get("properties", {})
        name = None
        for field in name_field_candidates:
            if props.get(field):
                name = props[field]
                break
        if name is None:
            name = "(tanpa nama)"
        geom = shape(feat["geometry"])
        result.append((name, prep(geom), geom))
    return result


def find_containing(point: Point, boundaries: list[tuple]):
    for name, prepared, geom in boundaries:
        if prepared.contains(point) or prepared.intersects(point):
            return name
    return None


# Mapping kode F_KAW -> label yang lebih dibaca manusia
FUNGSI_KAWASAN_LABEL = {
    "HL": "Hutan Lindung",
    "HP": "Hutan Produksi Tetap",
    "HPT": "Hutan Produksi Terbatas",
    "HPK": "Hutan Produksi Konversi",
    "APL": "Areal Penggunaan Lain",
    "TN": "Taman Nasional",
    "TWA": "Taman Wisata Alam",
    "CA": "Cagar Alam",
    "CAL": "Cagar Alam Laut",
    "SM": "Suaka Margasatwa",
    "TB": "Taman Buru",
    "TAHURA": "Taman Hutan Raya",
    "0": "Tidak Terklasifikasi",
}


def main():
    print("=== Build dashboard data BPHL Lampung ===")
    print(f"Waktu run (UTC): {datetime.now(timezone.utc).isoformat()}")

    print("\nMemuat boundary lokal ...")
    kph_boundaries = load_boundary("batas_kph.geojson", ["ORGANISASI"])
    pbph_boundaries = load_boundary("batas_pbph.geojson", ["NAMOBJ"])
    fungsi_boundaries = load_boundary("kawasan_fungsi_hutan.geojson", ["F_KAW"])
    print(f"  KPH: {len(kph_boundaries)} poligon")
    print(f"  PBPH: {len(pbph_boundaries)} poligon")
    print(f"  Fungsi kawasan hutan: {len(fungsi_boundaries)} poligon")

    print("\nMengambil hotspot dari NASA FIRMS ...")
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
    stat_per_kph = Counter()
    stat_per_pbph = Counter()
    stat_per_fungsi = Counter()
    stat_per_confidence = Counter()

    for row in hotspots_raw:
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (KeyError, ValueError):
            continue
        pt = Point(lon, lat)

        kph_name = find_containing(pt, kph_boundaries)
        pbph_name = find_containing(pt, pbph_boundaries)
        fungsi_code = find_containing(pt, fungsi_boundaries)
        fungsi_label = FUNGSI_KAWASAN_LABEL.get(fungsi_code, fungsi_code)

        conf_raw = (row.get("confidence") or "").strip().lower()
        if conf_raw in ("l", "n", "h"):
            conf_label = {"l": "low", "n": "medium", "h": "high"}[conf_raw]
        else:
            try:
                conf_val = float(conf_raw)
                conf_label = "high" if conf_val >= 80 else "medium"
            except ValueError:
                conf_label = "unknown"

        props = {
            "acq_date": row.get("acq_date"),
            "acq_time": row.get("acq_time"),
            "satellite": row.get("satellite", row.get("_source")),
            "instrument": row.get("instrument"),
            "confidence": conf_label,
            "frp": row.get("frp"),
            "daynight": row.get("daynight"),
            "kph": kph_name or "Luar KPH",
            "pbph": pbph_name or "Luar PBPH",
            "fungsi_kawasan": fungsi_label or "Tidak Diketahui",
        }
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })

        stat_per_kph[props["kph"]] += 1
        stat_per_pbph[props["pbph"]] += 1
        stat_per_fungsi[props["fungsi_kawasan"]] += 1
        stat_per_confidence[props["confidence"]] += 1

    hotspots_fc = {"type": "FeatureCollection", "features": features}
    out_path = os.path.join(DATA_DIR, "hotspots.geojson")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(hotspots_fc, f, ensure_ascii=False)
    print(f"\nDitulis: {out_path} ({len(features)} titik)")

    stats = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_hotspot": len(features),
        "per_kph": dict(stat_per_kph.most_common()),
        "per_pbph": dict(stat_per_pbph.most_common()),
        "per_fungsi_kawasan": dict(stat_per_fungsi.most_common()),
        "per_confidence": dict(stat_per_confidence),
    }
    stats_path = os.path.join(DATA_DIR, "stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"Ditulis: {stats_path}")
    print("\nRingkasan per KPH (5 teratas):")
    for name, count in stat_per_kph.most_common(5):
        print(f"  {name}: {count}")


if __name__ == "__main__":
    main()
