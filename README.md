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

## Net-new floodplain vs. the prior map: "effective" and "projected"

Two scripts measure land that is *now* in a floodplain and was not before.

| Status | Meaning | Script |
|---|---|---|
| `effective` | Changes that took legal effect in the target year (LOMRs), compared with the prior map rebuilt from FEMA Map Service Center archived FIRM databases | `effective_change_diff.py <DFIRM_ID> --label ST_County_FIPS` |
| `projected` | A county's **draft** maps used as the new version, compared with today's effective map. These maps are not final and have no regulatory effect. | `projected_harris_maapnext.py --nfhl <unzipped NFHL county download>` (Harris County, TX) |

For a county with a draft map, the draft is used as the new version and results are labeled `projected`.

### 2026 five-county run: `data/five_county_summary_2026.csv`

| County | Status | Newly 100-yr (ac) | Newly 500-yr (ac) | Removed from 100-yr (ac) | Parcels newly 100-yr | Parcels newly 500-yr |
|---|---|---|---|---|---|---|
| Harris, TX | projected (MAAPnext draft) | 100,410 (40,610 of it was 500-yr) | 97,330 | 107,873 | 87,666 | 135,199 |
| Hamilton, TN | effective (LOMR 25-04-3371P) | 32.8 | 21.3 | 48.5 | n/a | n/a |
| Escambia, FL | effective | 0 (no 2026 changes) | 0 | 0 | 0 | 0 |
| Fulton, GA | effective | 0 (no 2026 changes) | 0 | 0 | 0 | 0 |
| Pender, NC | effective | 0 (no 2026 changes) | 0 | 0 | 0 | 0 |

Notes:
* Harris draft classes: High Risk and Extreme Risk are treated as 100-yr;
  Moderate Risk as 500-yr. The MAAPnext-only "Community Flooding" class
  (126,703 acres) is excluded because it is not a FEMA flood zone.
* Harris parcels (Harris Central Appraisal District, 1.47M parcels) are
  classified at their centroid. Published estimates that count parcels
  *partially* in the floodplain will be higher.
* Hamilton's comparison uses FEMA's 11/28/2025 FIRM database. That covers
  100% of the revision area, so the baseline is the map as of late 2025.
* "Chattanooga County, TN" was interpreted as Hamilton County, TN (Chattanooga
  is its county seat).

Outputs:
* `data/projected/TX_Harris_48201/` contains `summary.csv`,
  `parcels_projected_changes.csv.gz` (addresses, current vs draft zone),
  `newly_added_zones.gpkg.gz` and `map.png`.
* `data/effective_diff/TN_Hamilton_47065/` contains
  `effective_changes_2026.csv` and `.gpkg`.
