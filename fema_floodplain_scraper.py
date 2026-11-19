#!/usr/bin/env python3
"""
FEMA floodplain change scraper -- nationwide, broken out by county.

Finds every flood-map change that became effective in a given year and pulls
the 100-year (1% annual chance / SFHA) and 500-year (0.2% annual chance,
"shaded X") flood-zone polygons those changes drew.

Source: FEMA National Flood Hazard Layer (NFHL) ArcGIS REST service
  https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer
    layer 1  = LOMRs (Letters of Map Revision), with EFF_DATE
    layer 3  = FIRM Panels, with EFF_DATE (new / revised map panels)
    layer 28 = Flood Hazard Zones (S_FLD_HAZ_AR), with SOURCE_CIT
    table 41 = Study_Info (state / county names)

How "new in year X" is determined
---------------------------------
Each flood-zone polygon carries SOURCE_CIT, the citation of the study or
LOMR that produced it (e.g. "26081C_LOMC9"). A LOMR or FIRM panel that
became effective in the target year has its own SOURCE_CIT. Zone polygons
with a matching citation are the zones that change drew.

For panels whose citation is only a basemap citation (e.g. "..._BASE1"),
the zone polygons intersecting those panels are taken instead
(change_type = "new_panel_area").

IMPORTANT: The NFHL holds only the *current* effective map. A polygon drawn
by a 2026 revision may partly overlap land that was already in the
floodplain under the old map. To isolate only the newly added land, pass
--baseline with a snapshot from an earlier run (or any older NFHL extract).
The geometry already in the floodplain is then subtracted.

Outputs (in --out directory)
----------------------------
  summary_by_county.csv          one row per county
  changes_<year>.csv             one row per LOMR / panel group
  counties/<ST>_<County>_<FIPS>.geojson   zone polygons per county
  all_zones_<year>.gpkg          everything in one GeoPackage (layer "zones")
  parcels_<year>.csv             (only with --parcels) parcels touching new zones
  cache/                         raw API responses (makes reruns resumable)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
LYR_LOMR, LYR_PANEL, LYR_ZONE, LYR_POL, TBL_STUDY = 1, 3, 28, 22, 41
PAGE = 1000  # below the server max (2000) to keep responses small and reliable

ZONE_FIELDS = "DFIRM_ID,FLD_AR_ID,FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE,DEPTH,SOURCE_CIT"

STATE_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}
ABBR_STATE = {v: k for k, v in STATE_ABBR.items()}

log = logging.getLogger("fema")
logging.getLogger("pyogrio").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# HTTP with retry + on-disk cache (the FEMA server resets connections often)
# --------------------------------------------------------------------------- #
class Client:
    def __init__(self, cache_dir: Path, retries: int = 10, timeout: int = 180):
        self.cache = cache_dir
        self.cache.mkdir(parents=True, exist_ok=True)
        self.retries, self.timeout = retries, timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "fema-floodplain-scraper/1.0"

    def get(self, path: str, use_cache: bool = True, method: str = "GET", retries: int | None = None,
            **params) -> dict:
        params = {"f": "json", **params}
        key = hashlib.sha1(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
        cf = self.cache / f"{key}.json"
        if use_cache and cf.exists():
            return json.loads(cf.read_text())
        url = f"{BASE}/{path}"
        last = None
        for attempt in range(retries or self.retries):
            try:
                if method == "POST":
                    r = self.s.post(url, data=params, timeout=self.timeout)
                else:
                    r = self.s.get(url, params=params, timeout=self.timeout)
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    raise RuntimeError(j["error"])
                if use_cache:
                    cf.write_text(json.dumps(j))
                return j
            except Exception as e:  # noqa: BLE001
                last = e
                wait = min(60, 2 * 1.6 ** attempt) + random.random()
                log.debug("retry %s %s (%s) in %.1fs", path, attempt + 1, e, wait)
                time.sleep(wait)
        raise RuntimeError(f"giving up on {path} {params.get('where','')}: {last}")

    def query_all(self, layer: int, where: str, out_fields: str = "*", geometry: bool = False,
                  extra: dict | None = None) -> list[dict]:
        """Page through a layer query. Returns GeoJSON-like features or attribute dicts."""
        out, offset, page = [], 0, PAGE
        while True:
            p = dict(where=where, outFields=out_fields, returnGeometry=str(geometry).lower(),
                     resultOffset=offset, resultRecordCount=page, orderByFields="OBJECTID")
            if geometry:
                p.update(outSR=4326, f="geojson")
            if extra:
                p.update(extra)
            try:
                # large/complex polygons can make the server 500 -- retry with smaller pages
                j = self.get(f"{layer}/query", retries=4 if page > 5 else None, **p)
            except RuntimeError:
                if page <= 5:
                    raise
                page = max(5, page // 5)
                if geometry:
                    extra = {**(extra or {}), "geometryPrecision": 6}  # ~10 cm, shrinks payloads
                log.info("shrinking page size to %d for %s", page, where[:80])
                continue
            feats = j.get("features", [])
            if geometry:
                out.extend(feats)
            else:
                out.extend(f["attributes"] for f in feats)
            exceeded = j.get("exceededTransferLimit") or j.get("properties", {}).get("exceededTransferLimit")
            if not feats or (not exceeded and len(feats) < page):
                break
            offset += len(feats)
        return out


# --------------------------------------------------------------------------- #
# Classification helpers
# --------------------------------------------------------------------------- #
def flood_class(attrs: dict) -> str | None:
    """Return '100yr', '500yr' or None (outside both)."""
    if (attrs.get("SFHA_TF") or "").upper() == "T":
        return "100yr"
    zone = (attrs.get("FLD_ZONE") or "").upper()
    sub = (attrs.get("ZONE_SUBTY") or "").upper()
    if zone == "X" and ("0.2 PCT" in sub or "LEVEE" in sub or "1 PCT DEPTH LESS" in sub
                        or "1 PCT DRAINAGE AREA" in sub or "1 PCT CONTRIBUTING" in sub):
        return "500yr"
    return None


FLOOD_WHERE = ("(SFHA_TF = 'T' OR (FLD_ZONE = 'X' AND (ZONE_SUBTY LIKE '0.2 PCT%' "
               "OR ZONE_SUBTY LIKE '%LEVEE%' OR ZONE_SUBTY LIKE '1 PCT%')))")

BASEMAP_CIT = re.compile(r"_(BASE|BASEMAP|ORTHO|IMAGERY)\d*$", re.I)


def esc(s: str) -> str:
    return s.replace("'", "''")


def ms_to_date(ms) -> str:
    if ms in (None, ""):
        return ""
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def date_where(year: int, start: str | None, end: str | None) -> str:
    s = start or f"{year}-01-01"
    e = end or f"{year + 1}-01-01"
    return f"EFF_DATE >= DATE '{s}' AND EFF_DATE < DATE '{e}'"


def state_filter(states: list[str] | None) -> str:
    if not states:
        return ""
    fips = [ABBR_STATE.get(s.upper(), s) for s in states]
    return " AND (" + " OR ".join(f"DFIRM_ID LIKE '{f}%'" for f in fips) + ")"


def collect_changes(cli: Client, where: str) -> list[dict]:
    """Return change records: one per LOMR, one per (DFIRM_ID, SOURCE_CIT) panel group."""
    changes = []
    lomrs = cli.query_all(LYR_LOMR, where, "DFIRM_ID,CASE_NO,SOURCE_CIT,EFF_DATE,STATUS,LOMR_ID")
    log.info("LOMRs effective in window: %d", len(lomrs))
    for l in lomrs:
        changes.append(dict(change_type="LOMR", dfirm_id=l["DFIRM_ID"], source_cit=l["SOURCE_CIT"],
                            case_no=l.get("CASE_NO") or "", eff_date=ms_to_date(l["EFF_DATE"]),
                            panels="", lomr_id=l.get("LOMR_ID") or ""))

    panels = cli.query_all(LYR_PANEL, where, "OBJECTID,DFIRM_ID,FIRM_PAN,SOURCE_CIT,EFF_DATE,PNP_REASON")
    log.info("FIRM panels effective in window: %d", len(panels))
    groups: dict[tuple, list] = defaultdict(list)
    for p in panels:
        groups[(p["DFIRM_ID"], p["SOURCE_CIT"])].append(p)
    for (dfirm, cit), ps in groups.items():
        printed = [p for p in ps if not p.get("PNP_REASON")]
        changes.append(dict(
            change_type="new_panel_area" if BASEMAP_CIT.search(cit or "") else "map_revision",
            dfirm_id=dfirm, source_cit=cit, case_no="",
            eff_date=max(ms_to_date(p["EFF_DATE"]) for p in ps),
            panels=";".join(sorted(p["FIRM_PAN"] for p in ps if p.get("FIRM_PAN"))),
            panel_oids=[p["OBJECTID"] for p in printed], lomr_id=""))
    return changes


def fetch_zones_for_change(cli: Client, ch: dict) -> list[dict]:
    if ch["change_type"] in ("LOMR", "map_revision"):
        w = f"DFIRM_ID = '{esc(ch['dfirm_id'])}' AND SOURCE_CIT = '{esc(ch['source_cit'])}' AND {FLOOD_WHERE}"
        feats = cli.query_all(LYR_ZONE, w, ZONE_FIELDS, geometry=True)
        if ch["change_type"] == "LOMR" and not feats and ch.get("lomr_id"):
            # Fallback: zones spatially inside the LOMR boundary
            feats = zones_in_layer_shapes(cli, LYR_LOMR, f"LOMR_ID = '{esc(ch['lomr_id'])}'", ch["dfirm_id"])
            for f in feats:
                f["properties"]["_spatial_fallback"] = True
        return feats
    # basemap-only panel citation: take zones intersecting those panels
    oids = ch.get("panel_oids") or []
    if not oids:
        return []
    out = []
    for i in range(0, len(oids), 20):
        out += zones_in_layer_shapes(cli, LYR_PANEL, "OBJECTID IN (" + ",".join(map(str, oids[i:i + 20])) + ")",
                                     ch["dfirm_id"])
    return out


def zones_in_layer_shapes(cli: Client, layer: int, where: str, dfirm: str) -> list[dict]:
    """Zones in DFIRM that intersect the shapes selected from another layer.

    The query sends each shape's bounding box (GET-friendly) and the exact
    intersection test is done locally with shapely.
    """
    from shapely.geometry import shape

    shp = cli.get(f"{layer}/query", where=where, returnGeometry="true", outFields="OBJECTID",
                  outSR=4326, f="geojson")
    out, seen = [], set()
    for f in shp.get("features", []):
        if not f.get("geometry"):
            continue
        sg = shape(f["geometry"])
        x0, y0, x1, y1 = sg.bounds
        feats = cli.query_all(LYR_ZONE, f"DFIRM_ID = '{esc(dfirm)}' AND {FLOOD_WHERE}", ZONE_FIELDS,
                              geometry=True, extra=dict(geometry=f"{x0},{y0},{x1},{y1}",
                                                       geometryType="esriGeometryEnvelope",
                                                       spatialRel="esriSpatialRelIntersects", inSR=4326))
        for z in feats:
            if not z.get("geometry") or not sg.intersects(shape(z["geometry"])):
                continue
            k = z["properties"].get("FLD_AR_ID") or json.dumps(z["geometry"])[:200]
            if k not in seen:
                seen.add(k)
                out.append(z)
    return out


def county_lookup(cli: Client, dfirm_ids: set[str]) -> dict[str, dict]:
    """DFIRM_ID -> {state_fips, county_fips, state, county}."""
    res = {}
    ids = sorted(dfirm_ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        w = "DFIRM_ID IN (" + ",".join(f"'{esc(d)}'" for d in chunk) + ")"
        for r in cli.query_all(TBL_STUDY, w, "DFIRM_ID,STATE_NM,CNTY_NM,STUDY_NM,CW_TF,INDX_EFFDT"):
            res.setdefault(r["DFIRM_ID"], r)
    out = {}
    for d in ids:
        st = d[:2]
        r = res.get(d, {})
        if d.upper().endswith("C"):  # countywide DFIRM: SSCCCC
            co = d[2:5]
        else:  # community-based DFIRM: look up county from Political Jurisdictions
            pj = cli.query_all(LYR_POL, f"DFIRM_ID = '{esc(d)}'", "CO_FIPS,ST_FIPS")
            cos = [p["CO_FIPS"] for p in pj if p.get("CO_FIPS")]
            co = max(set(cos), key=cos.count) if cos else "000"
        out[d] = dict(state_fips=st, county_fips=f"{st}{co}", state_abbr=STATE_ABBR.get(st, st),
                      state=r.get("STATE_NM") or STATE_ABBR.get(st, st),
                      county=r.get("CNTY_NM") or r.get("STUDY_NM") or f"County {co}",
                      firm_index_eff=ms_to_date(r.get("INDX_EFFDT")))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=time.gmtime().tm_year)
    ap.add_argument("--start", help="override start date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", help="override end date YYYY-MM-DD (exclusive)")
    ap.add_argument("--states", nargs="*", help="limit to states, e.g. TX LA 48")
    ap.add_argument("--out", default="output")
    ap.add_argument("--workers", type=int, default=4, help="parallel requests (keep low; FEMA server is fragile)")
    ap.add_argument("--no-panels", action="store_true", help="only LOMRs (skip new/revised FIRM panels)")
    ap.add_argument("--baseline", help="older floodplain polygons (gpkg/geojson/shp) to subtract -> net-new land only")
    ap.add_argument("--parcels", nargs="*", help="parcel files (gpkg/geojson/shp/parquet) to intersect")
    ap.add_argument("--parcel-id-field", default=None, help="parcel ID column (auto-detected if omitted)")
    ap.add_argument("--max-changes", type=int, help="debug: process only first N changes")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    out = Path(a.out)
    (out / "counties").mkdir(parents=True, exist_ok=True)
    cli = Client(out / "cache")

    where = date_where(a.year, a.start, a.end) + state_filter(a.states)
    log.info("Query window: %s", where)
    changes = collect_changes(cli, where)
    if a.no_panels:
        changes = [c for c in changes if c["change_type"] == "LOMR"]
    if a.max_changes:
        changes = changes[: a.max_changes]
    counties = county_lookup(cli, {c["dfirm_id"] for c in changes})

    # ---- fetch zones ---------------------------------------------------- #
    # Each change's raw GeoJSON is converted to a compact GeoDataFrame inside the
    # worker, so raw dicts never pile up (a nationwide run is >100k polygons).
    def fetch_gdf(ch):
        import geopandas as gpd
        feats = [f for f in fetch_zones_for_change(cli, ch) if f.get("geometry")]
        if not feats:
            return None
        cty = counties[ch["dfirm_id"]]
        for f in feats:
            p = f["properties"]
            p.update(flood_class=flood_class(p), change_type=ch["change_type"], case_no=ch["case_no"],
                     change_cit=ch["source_cit"], eff_date=ch["eff_date"], county_fips=cty["county_fips"],
                     county=cty["county"], state=cty["state_abbr"])
        g = gpd.GeoDataFrame.from_features(feats, crs=4326)
        g["geometry"] = g.geometry.make_valid()
        return g

    parts, n_polys, done = [], 0, 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch_gdf, c): c for c in changes}
        for fut in as_completed(futs):
            ch = futs[fut]
            done += 1
            try:
                g = fut.result()
            except Exception as e:  # noqa: BLE001
                log.error("failed %s %s: %s", ch["dfirm_id"], ch["source_cit"], str(e)[:300])
                ch["error"] = str(e)[:500]
                continue
            ch["n_zones"] = 0 if g is None else len(g)
            if g is not None:
                parts.append(g)
                n_polys += len(g)
            if done % 25 == 0 or done == len(changes):
                log.info("changes processed %d/%d, zone polygons so far %d", done, len(changes), n_polys)

    write_outputs(out, a, changes, counties, parts)


def write_outputs(out: Path, a, changes, counties, parts):
    import csv

    import geopandas as gpd
    import pandas as pd
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    if parts:
        gdf = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326)
        parts.clear()
        # the same polygon can be reached via both a LOMR and a panel group: keep first
        gdf = gdf.drop_duplicates(subset=["FLD_AR_ID", "DFIRM_ID"], keep="first")
    else:
        gdf = gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=4326)

    if a.baseline and len(gdf):
        log.info("Subtracting baseline floodplain from %s", a.baseline)
        base = gpd.read_file(a.baseline).to_crs(4326)
        if "flood_class" not in base.columns:
            base["flood_class"] = [flood_class(r) for r in base.to_dict("records")]
        parts = []
        for cls in ("100yr", "500yr"):
            g = gdf[gdf.flood_class == cls].copy()
            # land that was already 100yr counts as "already in" for both classes
            prior = base[base.flood_class.isin(["100yr"] if cls == "100yr" else ["100yr", "500yr"])]
            if len(g) and len(prior):
                g = g.overlay(prior[["geometry"]], how="difference", keep_geom_type=True)
            parts.append(g)
        gdf = pd.concat(parts, ignore_index=True)
        gdf = gdf[~gdf.geometry.is_empty]
        gdf["net_new"] = True

    if len(gdf):
        gdf["acres"] = [abs(geod.geometry_area_perimeter(g)[0]) / 4046.8564224 for g in gdf.geometry]
        gdf.to_file(out / f"all_zones_{a.year}.gpkg", layer="zones", driver="GPKG")
        for fips, g in gdf.groupby("county_fips"):
            r = g.iloc[0]
            g.to_file(out / "counties" / f"{r.state}_{safe(r.county)}_{fips}.geojson", driver="GeoJSON")

    # ---- change list --------------------------------------------------- #
    with open(out / f"changes_{a.year}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["state", "county", "county_fips", "dfirm_id", "change_type", "case_no", "source_cit",
                    "eff_date", "panels", "n_zone_polygons", "error"])
        for c in sorted(changes, key=lambda c: (counties[c["dfirm_id"]]["county_fips"], c["eff_date"])):
            k = counties[c["dfirm_id"]]
            w.writerow([k["state_abbr"], k["county"], k["county_fips"], c["dfirm_id"], c["change_type"],
                        c["case_no"], c["source_cit"], c["eff_date"], c["panels"], c.get("n_zones", ""),
                        c.get("error", "")])

    # ---- parcels -------------------------------------------------------- #
    parcel_counts = defaultdict(lambda: {"100yr": 0, "500yr": 0})
    if a.parcels and len(gdf):
        parcel_counts = intersect_parcels(out, a, gdf)

    # ---- county summary ------------------------------------------------ #
    summ = defaultdict(lambda: defaultdict(float))
    for c in changes:
        k = counties[c["dfirm_id"]]
        s = summ[k["county_fips"]]
        s["_state"], s["_county"] = k["state_abbr"], k["county"]
        s["lomrs"] += c["change_type"] == "LOMR"
        s["panel_groups"] += c["change_type"] != "LOMR"
        s["latest_eff"] = max(s.get("latest_eff") or "", c["eff_date"])
    if len(gdf):
        for (fips, cls), g in gdf.groupby(["county_fips", "flood_class"]):
            summ[fips][f"acres_{cls}"] += g.acres.sum()
            summ[fips][f"polys_{cls}"] += len(g)
    with open(out / "summary_by_county.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        hdr = ["state", "county", "county_fips", "lomrs", "panel_groups", "latest_eff_date",
               "zone_polys_100yr", "acres_100yr", "zone_polys_500yr", "acres_500yr"]
        if a.parcels:
            hdr += ["parcels_100yr", "parcels_500yr"]
        w.writerow(hdr)
        for fips in sorted(summ):
            s = summ[fips]
            row = [s["_state"], s["_county"], fips, int(s["lomrs"]), int(s["panel_groups"]), s["latest_eff"],
                   int(s["polys_100yr"]), round(s["acres_100yr"], 1), int(s["polys_500yr"]),
                   round(s["acres_500yr"], 1)]
            if a.parcels:
                row += [parcel_counts[fips]["100yr"], parcel_counts[fips]["500yr"]]
            w.writerow(row)
    log.info("Done. %d counties, %d changes, %d zone polygons -> %s", len(summ), len(changes), len(gdf), out)


def intersect_parcels(out: Path, a, zones):
    import geopandas as gpd
    import pandas as pd

    counts = defaultdict(lambda: {"100yr": 0, "500yr": 0})
    results = []
    zones = zones[["geometry", "flood_class", "county_fips", "county", "state", "FLD_ZONE", "change_type",
                   "case_no", "eff_date"]].reset_index(drop=True)
    for path in a.parcels:
        log.info("Intersecting parcels: %s", path)
        bbox = tuple(zones.total_bounds)
        par = (gpd.read_parquet(path) if path.endswith(".parquet") else gpd.read_file(path, bbox=bbox))
        par = par.to_crs(4326)
        idf = a.parcel_id_field or next((c for c in par.columns if c.lower() in
                                         ("parcel_id", "parcelid", "apn", "pin", "parno", "ll_uuid", "geoid",
                                          "parcelnumb", "objectid")), None)
        par = par.reset_index(drop=True)
        par["_pid"] = par[idf].astype(str) if idf else par.index.astype(str)
        import warnings
        with warnings.catch_warnings():  # area in degrees is only used for a ratio
            warnings.simplefilter("ignore")
            par["_parea"] = par.geometry.area
        hits = gpd.sjoin(par, zones, predicate="intersects", how="inner")
        if hits.empty:
            continue
        # fraction of the parcel covered (approximate, in degrees^2 -- fine for a ratio)
        zg = zones.geometry.values
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hits["pct_in_zone"] = [
                round(100 * geom.intersection(zg[i]).area / pa, 1) if pa else None
                for geom, i, pa in zip(hits.geometry.values, hits.index_right.values, hits._parea.values)]
        # keep the most severe class per parcel (100yr beats 500yr)
        hits["_rank"] = hits.flood_class.map({"100yr": 0, "500yr": 1})
        hits = hits.sort_values(["_pid", "_rank", "pct_in_zone"], ascending=[True, True, False])
        hits = hits.drop_duplicates("_pid")
        keep = [c for c in par.columns if c not in ("geometry", "_parea")][:25]
        hits["centroid_lat"] = hits.geometry.representative_point().y
        hits["centroid_lon"] = hits.geometry.representative_point().x
        results.append(pd.DataFrame(hits[keep + ["state", "county", "county_fips", "flood_class", "FLD_ZONE",
                                                 "pct_in_zone", "change_type", "case_no", "eff_date",
                                                 "centroid_lat", "centroid_lon"]]))
        for (fips, cls), n in hits.groupby(["county_fips", "flood_class"]).size().items():
            counts[fips][cls] += int(n)
    if results:
        df = pd.concat(results, ignore_index=True).rename(columns={"_pid": "parcel_id"})
        df.to_csv(out / f"parcels_{a.year}.csv", index=False)
        log.info("Parcels touching new/revised floodplain: %d", len(df))
    return counts


if __name__ == "__main__":
    sys.exit(main())
