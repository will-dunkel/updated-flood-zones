#!/usr/bin/env python3
"""
Net-new floodplain from map changes that took EFFECTIVE in a given year,
compared against an older map rebuilt from FEMA's archived map databases.
All results are labeled status = "effective".

For one county (FEMA DFIRM_ID, e.g. 48201C):
  1. List the county's LOMRs (map revisions) effective in --year from the
     NFHL map service.
  2. Search the FEMA Map Service Center for the county's dated "FIRM Database"
     packages, download them, and read their flood-zone layer (S_FLD_HAZ_AR).
  3. For each revision area, build the prior map from the NEWEST package
     covering it. Exclude any part touched by a revision issued after that
     package and before the target year: the archive has no GIS for it, so it
     is reported as `baseline_unknown`, not guessed.
  4. Compare the year's 100-yr / 500-yr zones with the prior map:
     newly_100yr, newly_500yr, removed_100yr.

Outputs (--out/<ST>_<County>_<FIPS>/):
  effective_changes_<year>.csv    one row per revision (acres per category)
  effective_changes_<year>.gpkg   polygons per category
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import logging
import sys
import time
import zipfile
from pathlib import Path

import requests

NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
MSC = "https://msc.fema.gov/portal"
AC = 4046.8564224
log = logging.getLogger("effdiff")


def get(url, method="GET", tries=10, **kw):
    last = None
    for i in range(tries):
        try:
            r = requests.request(method, url, timeout=600, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(30, 2 * 1.6 ** i))
    raise RuntimeError(f"{url}: {last}")


def msc_firm_dbs(dfirm: str) -> list[dict]:
    st, co = dfirm[:2], dfirm[:5]
    r = get(f"{MSC}/advanceSearch", "POST", headers={"X-Requested-With": "XMLHttpRequest"},
            data=dict(affiliate="fema", selstate=st, selcounty=co, selcommunity=dfirm, searchedCid=dfirm,
                      method="search"))
    rows = (r.json().get("EFFECTIVE") or {}).get("FIRM_DB") or []
    out = [dict(name=x["product_NAME"], date=x.get("product_EFFECTIVE_DATE_STRING"), size=x.get("product_FILE_SIZE"))
           for x in rows if str(x.get("product_NAME", "")).startswith(dfirm)]
    log.info("MSC FIRM database packages for %s: %s", dfirm, [(o["name"], o["date"]) for o in out])
    return out


def download_pkg(name: str, dest: Path) -> Path:
    d = dest / name
    if d.exists() and any(d.rglob("*")):
        return d
    r = get(f"{MSC}/downloadProduct", params=dict(productTypeID="FINAL_PRODUCT", productSubTypeID="FIRM_DB",
                                                  productID=name))
    d.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(r.content)).extractall(d)
    return d


def nfhl_lomrs(dfirm: str):
    import geopandas as gpd
    import pandas as pd
    j = get(f"{NFHL}/1/query", params=dict(f="geojson", where=f"DFIRM_ID='{dfirm}'", outFields="CASE_NO,EFF_DATE,SOURCE_CIT",
                                            outSR=4326)).json()
    g = gpd.GeoDataFrame.from_features(j["features"], crs=4326).to_crs(5070)
    g["eff"] = pd.to_datetime(g.EFF_DATE, unit="ms")
    g["geometry"] = g.geometry.make_valid()
    return g


def zones_for_citation(dfirm: str, cit: str):
    import geopandas as gpd
    feats, off = [], 0
    while True:
        j = get(f"{NFHL}/28/query", params=dict(f="geojson", where=f"DFIRM_ID='{dfirm}' AND SOURCE_CIT='{cit}'",
                                                 outFields="FLD_ZONE,ZONE_SUBTY,SFHA_TF", outSR=4326,
                                                 resultOffset=off, resultRecordCount=200, geometryPrecision=6)).json()
        feats += j.get("features", [])
        if len(j.get("features", [])) < 200:
            break
        off += 200
    if not feats:
        return gpd.GeoDataFrame(columns=["geometry", "cls"], geometry="geometry", crs=5070)
    g = gpd.GeoDataFrame.from_features(feats, crs=4326).to_crs(5070)
    g["geometry"] = g.geometry.make_valid()
    g["cls"] = [cls_of(r) for r in g.to_dict("records")]
    return g


def cls_of(r: dict):
    z = str(r.get("FLD_ZONE") or "").upper()
    sub = str(r.get("ZONE_SUBTY") or "").upper()
    if str(r.get("SFHA_TF") or "").upper() == "T":
        return "100yr"
    if "0.2 PCT" in z or (z == "X" and ("0.2 PCT" in sub or "LEVEE" in sub or "1 PCT" in sub)):
        return "500yr"
    return None


def load_pkg(d: Path, name: str):
    import geopandas as gpd
    import pandas as pd
    from shapely.ops import unary_union
    shp = [p for p in glob.glob(f"{d}/**/*", recursive=True) if p.lower().endswith("s_fld_haz_ar.shp")]
    if not shp:
        return None
    g = gpd.read_file(shp[0]).to_crs(5070)
    g["geometry"] = g.geometry.make_valid()
    g["cls"] = [cls_of(r) for r in g.to_dict("records")]
    stamp = name.split("_")[1] if "_" in name and name.split("_")[1].isdigit() else None
    return dict(name=name, date=pd.Timestamp(stamp) if stamp else None, cov=g.union_all(),
                f100=unary_union(g[g.cls == "100yr"].geometry), f500=unary_union(g[g.cls == "500yr"].geometry))


def main(argv=None):
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dfirm", help="FEMA DFIRM_ID, e.g. 47065C")
    ap.add_argument("--label", required=True, help='output folder name, e.g. "TN_Hamilton_47065"')
    ap.add_argument("--year", type=int, default=time.gmtime().tm_year)
    ap.add_argument("--out", default="data/effective_diff")
    ap.add_argument("--cache", default="output/msc_cache")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    out = Path(a.out) / a.label
    out.mkdir(parents=True, exist_ok=True)

    l = nfhl_lomrs(a.dfirm)
    start = pd.Timestamp(f"{a.year}-01-01")
    new = l[(l.eff >= start) & (l.eff < pd.Timestamp(f"{a.year + 1}-01-01"))]
    log.info("%s: %d LOMRs effective in %d", a.dfirm, len(new), a.year)
    pkgs = []
    for p in msc_firm_dbs(a.dfirm):
        try:
            pk = load_pkg(download_pkg(p["name"], Path(a.cache)), p["name"])
        except Exception as e:  # noqa: BLE001
            log.warning("package %s failed: %s", p["name"], e)
            continue
        if pk:
            if pk["date"] is None and p["date"]:
                pk["date"] = pd.Timestamp(p["date"])
            pkgs.append(pk)
    pkgs.sort(key=lambda p: p["date"], reverse=True)

    rows, geoms = [], []
    for _, r in new.sort_values("eff").iterrows():
        A = r.geometry
        z = zones_for_citation(a.dfirm, r.SOURCE_CIT)
        c100 = unary_union(z[z.cls == "100yr"].geometry).intersection(A)
        c500 = unary_union(z[z.cls == "500yr"].geometry).intersection(A)
        clean, o100, o500, used, remaining = Polygon(), Polygon(), Polygon(), [], A
        for pk in pkgs:
            part = remaining.intersection(pk["cov"])
            if part.is_empty or part.area < 1:
                continue
            later = unary_union(l[(l.eff > pk["date"]) & (l.eff < start) & l.intersects(part)].geometry)
            ok = part.difference(later) if not later.is_empty else part
            clean, used = clean.union(ok), used + [pk["name"]]
            o100, o500 = o100.union(pk["f100"].intersection(ok)), o500.union(pk["f500"].intersection(ok))
            remaining = remaining.difference(part)
        cats = {"newly_100yr": c100.intersection(clean).difference(o100),
                "newly_500yr": c500.intersection(clean).difference(o100.union(o500)),
                "removed_100yr": o100.difference(c100),
                "baseline_unknown": A.difference(clean)}
        rows.append(dict(case_no=r.CASE_NO, eff_date=str(r.eff.date()), status="effective", lomr_acres=round(A.area / AC, 1),
                         baseline_known_pct=round(100 * clean.area / A.area, 1) if A.area else 0,
                         baseline_packages=";".join(used) or "none", now_100yr_acres=round(c100.area / AC, 1),
                         **{f"{k}_acres": round(v.area / AC, 1) for k, v in cats.items()}))
        for k, v in cats.items():
            if not v.is_empty and v.area > 1:
                geoms.append(dict(case_no=r.CASE_NO, eff_date=str(r.eff.date()), status="effective", change=k,
                                  acres=v.area / AC, geometry=v))
    df = pd.DataFrame(rows)
    df.to_csv(out / f"effective_changes_{a.year}.csv", index=False)
    if geoms:
        gpd.GeoDataFrame(geoms, crs=5070).to_crs(4326).to_file(out / f"effective_changes_{a.year}.gpkg", driver="GPKG")
    print(df.to_string(index=False) if len(df) else f"no LOMRs effective in {a.year}")


if __name__ == "__main__":
    sys.exit(main())
