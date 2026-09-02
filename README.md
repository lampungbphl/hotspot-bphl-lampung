# Monitor Hotspot BPHL Lampung

Dashboard publik monitoring titik panas (hotspot) kebakaran hutan dan lahan di wilayah kerja
**BPHL Lampung**, breakdown per **KPH**, **PBPH**, dan **fungsi kawasan hutan**. Data hotspot
diperbarui otomatis setiap hari dari NASA FIRMS.

Repo ini **berdiri sendiri** — semua boundary (batas KPH, batas PBPH, fungsi kawasan hutan)
disimpan sebagai file GeoJSON statis di folder `data/`, **tanpa database eksternal (tanpa
Supabase)**. Dashboard-nya berupa halaman statis (Leaflet) yang bisa langsung di-hosting lewat
GitHub Pages.

## Cara kerja

1. **Boundary** (`data/batas_kph.geojson`, `data/batas_pbph.geojson`,
   `data/kawasan_fungsi_hutan.geojson`) sudah disiapkan di repo ini — hasil simplifikasi dari
   data sumber, dan direproyeksi ke WGS84 (lon/lat) karena data asli KPH/PBPH memakai UTM 47S.
   File-file ini jarang berubah, cukup diperbarui manual kalau ada revisi batas.
2. **Hotspot harian** (`data/hotspots.geojson`, `data/stats.json`) diambil dari NASA FIRMS
   (sumber VIIRS SNPP/NOAA-20/NOAA-21) setiap hari lewat GitHub Actions, hanya yang confidence
   **sedang (nominal) dan tinggi** yang dipakai, lalu di-spatial-join ke tiga boundary di atas.
3. **Dashboard** (`index.html`) adalah halaman statis Leaflet yang membaca langsung file-file
   GeoJSON tersebut — cocok untuk GitHub Pages, tidak perlu server backend.

## Setup awal (sekali saja)

1. **Buat repo baru** di GitHub bernama `bphl-lampung-hotspot`, lalu push semua isi folder ini.

2. **Dapatkan API key NASA FIRMS** (kalau belum punya): daftar di
   https://firms.modaps.eosdis.nasa.gov/api/map_key/

3. **Tambahkan GitHub Secret** di repo (Settings → Secrets and variables → Actions →
   New repository secret):
   - `FIRMS_API_KEY` — API key NASA FIRMS kamu

4. **Jalankan workflow secara manual sekali untuk tes**
   (tab Actions → pilih "Update Hotspot Dashboard" → Run workflow). Setelah itu workflow akan
   otomatis jalan tiap hari sesuai jadwal cron.

5. **Aktifkan GitHub Pages** — Settings → Pages → Source: `Deploy from a branch` →
   branch `main`, folder `/ (root)`. Dashboard akan tersedia di
   `https://<username>.github.io/bphl-lampung-hotspot/`.

## Struktur

```
.github/workflows/
  update-dashboard.yml        # harian: fetch FIRMS -> spatial join -> commit
scripts/
  build_dashboard_data.py     # ambil FIRMS, spatial join ke 3 boundary lokal
  requirements.txt
data/
  batas_kph.geojson           # boundary KPH (statis, upload manual)
  batas_pbph.geojson          # boundary PBPH (statis, upload manual)
  kawasan_fungsi_hutan.geojson  # boundary fungsi kawasan hutan (statis, upload manual)
  hotspots.geojson            # digenerate otomatis, jangan edit manual
  stats.json                  # digenerate otomatis, jangan edit manual
index.html                    # dashboard
```

## Yang mungkin perlu disesuaikan

- **Jadwal cron** di `.github/workflows/update-dashboard.yml` (default 01:00 UTC / 08:00 WIB).
- **Bbox FIRMS** (`BBOX` di `scripts/build_dashboard_data.py`) — sudah mencakup seluruh
  wilayah kerja (Lampung + Bengkulu + sebagian Sumsel/Jambi, mengikuti cakupan file boundary),
  bisa dipersempit kalau mau mempercepat fetch.
- **Ambang confidence** — VIIRS pakai kode huruf (`l`/`n`/`h`), saat ini `n` (nominal/sedang)
  dan `h` (tinggi) yang dipakai; kode di `VIIRS_CONFIDENCE_ALLOWED` dan `MODIS_CONFIDENCE_MIN`
  bisa disesuaikan.
- **Sumber satelit** (`SOURCES` di `build_dashboard_data.py`) — saat ini VIIRS SNPP + NOAA-20 +
  NOAA-21, bisa ditambah `MODIS_NRT` kalau perlu.

## Rencana lanjutan

Notifikasi ke n8n (lewat webhook dari GitHub Actions setiap ada hotspot baru) akan dibangun
di repo terpisah, menyusul setelah dashboard ini berjalan stabil.
