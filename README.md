# FEMA floodplain change scraper (nationwide, by county)

Pulls every FEMA flood-map change that became **effective in a given year**
(default: the current year). For each one it gets the **100-year** (1% annual
chance, SFHA zones A/AE/AH/AO/V/VE...) and **500-year** (0.2% annual chance,
"shaded X") flood-zone polygons that the change drew. Results are broken out
by county.

Data source: FEMA's public National Flood Hazard Layer (NFHL) map service,
`hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer`. No API key is
needed.

## Data in this repo (2026 run, generated 2026-11-19)

| Path | Contents |
|---|---|
| `data/2026/summary_by_county.csv` | 361 counties in 48 states: changes, polygon counts, acres for 100-yr and 500-yr zones |
| `data/2026/changes_2026.csv` | All 836 changes effective in 2026 (741 LOMRs, 95 panel groups) |
| `data/2026/counties/*.geojson.gz` | Zone polygons per county (336 counties with zones), gzipped GeoJSON |

Totals: about 1.14M acres of new or revised 100-yr floodplain and about 403K acres of
500-yr floodplain. These are upper bounds; see the limits below. The combined
GeoPackage and the API cache are not committed. Rerun the scraper to regenerate
them.

Read a county file:

```python
import geopandas as gpd
gdf = gpd.read_file("/vsigzip/data/2026/counties/MI_Kent_County_26081.geojson.gz")
```

Or decompress it: `gunzip -k data/2026/counties/*.gz`.

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
# nationwide, 2026
python3 fema_floodplain_scraper.py --year 2026 --out output_2026

# a few states only
python3 fema_floodplain_scraper.py --year 2026 --states TX LA FL --out output_gulf

# custom date window
python3 fema_floodplain_scraper.py --start 2026-06-01 --end 2026-12-01

# add parcels (any county parcel file or Regrid export: gpkg/geojson/shp/parquet)
python3 fema_floodplain_scraper.py --year 2026 --states MI \
    --parcels kent_parcels.gpkg wayne_parcels.gpkg --parcel-id-field PIN

# net-new land only: subtract an older floodplain snapshot
python3 fema_floodplain_scraper.py --year 2027 --baseline output_2026/all_zones_2026.gpkg
```

Every API response is cached in `<out>/cache/`, so an interrupted run resumes
where it stopped. Delete the cache to force fresh data.

## Outputs

| File | Contents |
|---|---|
| `summary_by_county.csv` | One row per county: number of LOMRs and panel groups, polygon count and acres for 100-yr and 500-yr zones (plus parcel counts if `--parcels` was given) |
| `changes_<year>.csv` | Every LOMR or map revision: county, case number, effective date, panels affected |
| `counties/<ST>_<County>_<FIPS>.geojson` | The flood-zone polygons for each county |
| `all_zones_<year>.gpkg` | All polygons in one GeoPackage (open it in QGIS or ArcGIS) |
| `parcels_<year>.csv` | Parcels touching the new or revised zones, with flood class, zone, % of the parcel covered, case number and a centroid point |

## How "added this year" is determined, and its limits

* **LOMRs** (Letters of Map Revision) effective in the window are matched to
  zone polygons through their source citation (`SOURCE_CIT`, e.g.
  `26081C_LOMC9`). If no polygon carries the citation, the polygons inside
  the LOMR boundary are used instead.
* **FIRM panels** effective in the window are grouped by study citation. The
  zone polygons produced by that study are pulled. For panels with only a
  basemap citation, the polygons intersecting those panels are pulled
  (`change_type = new_panel_area`).
* **The NFHL contains only the current map.** A polygon drawn by a 2026
  revision can include land that was already in the floodplain before. That
  makes these results "new or revised floodplain", which is an upper bound on
  newly added land. To get strictly net-new land, run with `--baseline`
  pointing at an older snapshot. Each run's `all_zones_<year>.gpkg` works as
  next year's baseline, and an archived NFHL download from FEMA's Map Service
  Center works too.
* Changes with `n_zone_polygons = 0` drew no 100- or 500-year zones. These
  are usually LOMRs that only *removed* floodplain or only changed flood
  elevations.
* **Preliminary or draft maps are not included.** Examples are the Harris
  County, TX draft maps, which have not taken effect. FEMA publishes those
  separately at
  `hazards.fema.gov/arcgis/rest/services/PrelimPending/Prelim_NFHL/MapServer`.
* **FEMA does not publish parcels.** Parcel results come only from the files
  you pass with `--parcels`.
