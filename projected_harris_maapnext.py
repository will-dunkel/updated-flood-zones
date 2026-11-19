#!/usr/bin/env python3
"""
Projected floodplain change for Harris County, TX: the MAAPnext DRAFT FEMA maps
compared against the current EFFECTIVE FEMA map. All results are labeled
status = "projected": the draft maps have no regulatory effect yet.

Sources
-------
Draft ("new") map: Harris County Flood Control District MAAPnext draft flood
  hazard zones, published as a tiled thematic raster (1 m; this script reads the
  4 m pyramid level). Raster classes:
      1 High Risk           -> treated as 100-yr (1% annual chance, SFHA)
      2 Extreme Risk        -> treated as 100-yr (floodway / highest hazard)
      3 Moderate Risk       -> treated as 500-yr (0.2% annual chance)
      4 Community Flooding  -> MAAPnext-only category (local/urban ponding).
                               NOT a FEMA SFHA; reported separately.
  https://tiledimageservices2.arcgis.com/nLl0k0Mja5hnSeSl/arcgis/rest/services/MAAPNext_FloodHazardZones/ImageServer

Effective ("old") map: FEMA NFHL county download for 48201C, from the FEMA Map
  Service Center (S_FLD_HAZ_AR). Pass the unzipped folder with --nfhl.

Parcels: Harris Central Appraisal District parcels, as published in the same
  MAAPnext viewer. Each parcel is classified at its centroid (an approximation
  of where the structure is).
  https://services2.arcgis.com/nLl0k0Mja5hnSeSl/arcgis/rest/services/HCAD_Parcels/FeatureServer/1

Outputs (--out)
---------------
  summary.csv                   acres and parcel counts per change category
  parcels_projected_changes.csv.gz   every parcel whose class changes, with address
  newly_added_zones.gpkg        polygons of newly added 100-yr / 500-yr land
  cache/                        raw tiles and parcel pages (makes reruns resumable)
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import logging
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests

IMG = ("https://tiledimageservices2.arcgis.com/nLl0k0Mja5hnSeSl/arcgis/rest/services/"
       "MAAPNext_FloodHazardZones/ImageServer")
PARCELS = ("https://services2.arcgis.com/nLl0k0Mja5hnSeSl/arcgis/rest/services/"
           "HCAD_Parcels/FeatureServer/1")
ORIGIN_X, ORIGIN_Y = -10682310.0906, 3525532.0364      # tile scheme origin (EPSG:3857)
EXTENT_W, EXTENT_H = 117265.0, 86447.0                 # service extent in metres
LEVEL, RES = 7, 4.0                                    # 4 m pyramid level
TILE = 256

# pixel codes used in the comparison grids
NONE, F100, F500, COMM = 0, 1, 2, 3
DRAFT_MAP = {0: NONE, 1: F100, 2: F100, 3: F500, 4: COMM}
LABEL = {NONE: "none", F100: "100yr", F500: "500yr", COMM: "community_flooding"}

log = logging.getLogger("harris")


def get(url, retries=8, **params):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=120)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(30, 2 * 1.6 ** i))
    raise RuntimeError(f"{url}: {last}")


# --------------------------------------------------------------------------- #
def load_draft(cache: Path, workers: int) -> np.ndarray:
    import lerc
    rows, cols = math.ceil(EXTENT_H / (TILE * RES)), math.ceil(EXTENT_W / (TILE * RES))
    grid = np.zeros((rows * TILE, cols * TILE), np.uint8)
    tdir = cache / "tiles"
    tdir.mkdir(parents=True, exist_ok=True)

    def fetch(rc):
        r, c = rc
        f = tdir / f"{r}_{c}.lerc"
        if f.exists():
            b = f.read_bytes()
        else:
            resp = get(f"{IMG}/tile/{LEVEL}/{r}/{c}")
            b = resp.content if resp is not None else b""
            f.write_bytes(b)
        if not b:
            return
        res = lerc.decode(b)
        if res[0] != 0:
            return
        data, mask = res[1], res[2]
        t = np.where(mask, data, 0).astype(np.uint8)
        grid[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE] = t

    todo = [(r, c) for r in range(rows) for c in range(cols)]
    log.info("draft raster: %d tiles at %.0f m", len(todo), RES)
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for _ in ex.map(fetch, todo):
            done += 1
            if done % 1000 == 0:
                log.info("  tiles %d/%d", done, len(todo))
    lut = np.zeros(256, np.uint8)
    for k, v in DRAFT_MAP.items():
        lut[k] = v
    return lut[grid]


def transform():
    from rasterio.transform import from_origin
    return from_origin(ORIGIN_X, ORIGIN_Y, RES, RES)


def load_effective(nfhl_dir: Path, shape) -> tuple[np.ndarray, np.ndarray]:
    import geopandas as gpd
    from rasterio.features import rasterize
    sys.path.insert(0, str(Path(__file__).parent))
    from fema_floodplain_scraper import flood_class

    shp = next(p for p in glob.glob(f"{nfhl_dir}/**/*", recursive=True) if p.upper().endswith("S_FLD_HAZ_AR.SHP"))
    z = gpd.read_file(shp).to_crs(3857)
    z["cls"] = [flood_class(r) for r in z[["SFHA_TF", "FLD_ZONE", "ZONE_SUBTY"]].to_dict("records")]
    log.info("effective zones: %d polygons (%s)", len(z), z.cls.value_counts(dropna=False).to_dict())
    eff = np.zeros(shape, np.uint8)
    t = transform()
    # paint 500-yr first, then 100-yr on top
    for cls, code in (("500yr", F500), ("100yr", F100)):
        g = z[z.cls == cls].geometry
        if len(g):
            rasterize(((geom, code) for geom in g), out=eff, transform=t)
    # study footprint = anything the effective map covers (incl. zone X) -> county mask
    foot = rasterize(((geom, 1) for geom in z.geometry), out_shape=shape, transform=t, dtype=np.uint8)
    return eff, foot.astype(bool)


def pixel_acres(shape) -> np.ndarray:
    """True ground area of a 4 m Web-Mercator pixel varies with latitude: cos^2(lat)."""
    ys = ORIGIN_Y - (np.arange(shape[0]) + 0.5) * RES
    lat = np.degrees(2 * np.arctan(np.exp(ys / 6378137.0)) - np.pi / 2)
    return (RES * RES) * np.cos(np.radians(lat)) ** 2 / 4046.8564224


CHANGES = {
    # name: (draft condition, effective condition)
    "newly_100yr_from_none": (lambda d: d == F100, lambda e: (e != F100) & (e != F500)),
    "newly_100yr_from_500yr": (lambda d: d == F100, lambda e: e == F500),
    "newly_500yr": (lambda d: d == F500, lambda e: (e != F100) & (e != F500)),
    "removed_from_100yr": (lambda d: d != F100, lambda e: e == F100),
    "removed_from_500yr": (lambda d: (d != F100) & (d != F500), lambda e: e == F500),
    "community_flooding_only": (lambda d: d == COMM, lambda e: (e != F100) & (e != F500)),
    "stays_100yr": (lambda d: d == F100, lambda e: e == F100),
}


def classify(d, e):
    out = np.full(np.shape(d), "unchanged", dtype=object)
    for name, (fd, fe) in CHANGES.items():
        out[fd(d) & fe(e)] = name
    return out


def polygonize(draft, eff, foot, out: Path):
    import geopandas as gpd
    from rasterio.features import shapes
    from shapely.geometry import shape as shp
    t = transform()
    recs = []
    for name in ("newly_100yr_from_none", "newly_100yr_from_500yr", "newly_500yr"):
        fd, fe = CHANGES[name]
        # process in horizontal bands to bound memory
        for y0 in range(0, draft.shape[0], 4096):
            sl = slice(y0, y0 + 4096)
            m = (fd(draft[sl]) & fe(eff[sl]) & foot[sl]).astype(np.uint8)
            if not m.any():
                continue
            from affine import Affine
            bt = t * Affine.translation(0, y0)
            for geom, v in shapes(m, mask=m.astype(bool), transform=bt):
                recs.append({"change": name, "status": "projected", "geometry": shp(geom)})
    g = gpd.GeoDataFrame(recs, crs=3857)
    if len(g):
        g = g.dissolve("change", as_index=False).explode(index_parts=False)
        g["acres"] = g.to_crs(5070).area / 4046.8564224
        g = g[g.acres >= 0.05]  # drop single-pixel speckle (< ~200 m^2)
        g.to_crs(4326).to_file(out, driver="GPKG", layer="newly_added")
    log.info("polygonized newly added land: %d polygons", len(g))


# --------------------------------------------------------------------------- #
def load_parcels(cache: Path, workers: int):
    import pandas as pd
    meta = get(f"{PARCELS}/query", f="json", where="1=1",
               outStatistics=json.dumps([{"statisticType": "min", "onStatisticField": "OBJECTID", "outStatisticFieldName": "lo"},
                                         {"statisticType": "max", "onStatisticField": "OBJECTID", "outStatisticFieldName": "hi"}])).json()
    lo, hi = meta["features"][0]["attributes"]["lo"], meta["features"][0]["attributes"]["hi"]
    pdir = cache / "parcels"
    pdir.mkdir(parents=True, exist_ok=True)
    fields = ("OBJECTID,LOWPARCELID,parcel_type,Acreage,site_str_num,site_str_pfx,site_str_name,site_str_sfx,"
              "site_str_sfx_dir,site_city,site_zip,tax_year")
    step = 2000

    def page(start):
        f = pdir / f"{start}.json"
        if f.exists():
            return json.loads(f.read_text())
        j = get(f"{PARCELS}/query", f="json", where=f"OBJECTID >= {start} AND OBJECTID < {start + step}",
                outFields=fields, returnGeometry="false", returnCentroid="true", outSR=3857).json()
        if "error" in j:
            raise RuntimeError(j["error"])
        rows = [{**ft["attributes"], "cx": (ft.get("centroid") or {}).get("x"), "cy": (ft.get("centroid") or {}).get("y")}
                for ft in j.get("features", [])]
        f.write_text(json.dumps(rows))
        return rows

    starts = list(range(lo, hi + 1, step))
    log.info("parcels: %d pages", len(starts))
    rows = []
    with ThreadPoolExecutor(workers) as ex:
        for i, r in enumerate(ex.map(page, starts)):
            rows.extend(r)
            if (i + 1) % 100 == 0:
                log.info("  parcel pages %d/%d", i + 1, len(starts))
    return pd.DataFrame(rows)


def main(argv=None):
    import pandas as pd
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nfhl", required=True, help="unzipped FEMA NFHL county download for 48201C")
    ap.add_argument("--out", default="data/projected/TX_Harris_48201")
    ap.add_argument("--cache", default="output/harris_projected_cache")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--skip-parcels", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    out, cache = Path(a.out), Path(a.cache)
    out.mkdir(parents=True, exist_ok=True)

    draft = load_draft(cache, a.workers)
    eff, foot = load_effective(Path(a.nfhl), draft.shape)
    pa = pixel_acres(draft.shape)

    def acres(mask):  # row sums x per-row pixel area (avoids a full float array)
        return float((mask.sum(axis=1) * pa).sum())

    summary = []
    for name, (fd, fe) in CHANGES.items():
        summary.append({"category": name, "acres": acres(fd(draft) & fe(eff) & foot)})
    for code in (F100, F500):
        summary.append({"category": f"effective_total_{LABEL[code]}", "acres": acres((eff == code) & foot)})
        summary.append({"category": f"draft_total_{LABEL[code]}", "acres": acres((draft == code) & foot)})
    polygonize(draft, eff, foot, out / "newly_added_zones.gpkg")

    if not a.skip_parcels:
        p = load_parcels(cache, a.workers)
        ok = p.cx.notna()
        col = ((p.loc[ok, "cx"] - ORIGIN_X) / RES).astype(int)
        row = ((ORIGIN_Y - p.loc[ok, "cy"]) / RES).astype(int)
        inb = (row >= 0) & (row < draft.shape[0]) & (col >= 0) & (col < draft.shape[1])
        idx = inb[inb].index
        p["draft_zone"], p["effective_zone"] = None, None
        d = draft[row[inb], col[inb]]
        e = eff[row[inb], col[inb]]
        p.loc[idx, "draft_zone"] = [LABEL[v] for v in d]
        p.loc[idx, "effective_zone"] = [LABEL[v] for v in e]
        p["change"] = "unknown_location"
        p.loc[idx, "change"] = classify(d, e)
        p["status"] = "projected"
        lon = np.degrees(p.cx / 6378137.0)
        lat = np.degrees(2 * np.arctan(np.exp(p.cy / 6378137.0)) - np.pi / 2)
        p["lat"], p["lon"] = lat.round(6), lon.round(6)
        p["address"] = (p[["site_str_num", "site_str_pfx", "site_str_name", "site_str_sfx", "site_str_sfx_dir"]]
                        .fillna("").astype(str).agg(" ".join, axis=1).str.split().str.join(" "))
        counts = p.change.value_counts()
        for k, v in counts.items():
            summary.append({"category": f"parcels_{k}", "parcels": int(v)})
        keep = p[~p.change.isin(["unchanged", "stays_100yr"])]
        cols = ["LOWPARCELID", "address", "site_city", "site_zip", "parcel_type", "Acreage", "effective_zone",
                "draft_zone", "change", "status", "lat", "lon"]
        with gzip.open(out / "parcels_projected_changes.csv.gz", "wt") as fh:
            keep[cols].sort_values(["change", "site_zip", "address"]).to_csv(fh, index=False)
        log.info("parcels: %d total, %d with a projected change", len(p), len(keep))

    s = pd.DataFrame(summary)
    s.insert(0, "status", "projected")
    s.insert(0, "county", "Harris County, TX (48201)")
    s.to_csv(out / "summary.csv", index=False)
    print(s.to_string(index=False))


if __name__ == "__main__":
    sys.exit(main())
