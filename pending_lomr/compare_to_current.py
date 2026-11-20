import sys, json, time, requests, warnings, numpy as np
import geopandas as gpd
from shapely.ops import unary_union
from shapely import affinity
warnings.filterwarnings("ignore")
D=sys.argv[1]; AC=43560.0
x=gpd.read_file(f"{D}/lomr_extracted.gpkg").to_crs(2240).set_index("layer").geometry
new100,new500,rev=x["new_100yr"],x["new_500yr"],x["revised_area"]
B="https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
lo0,la0,lo1,la1=-84.425,33.640,-84.388,33.672
feats=[];off=0
while True:
    for i in range(10):
        try:
            j=requests.get(B,params=dict(f="geojson",where="DFIRM_ID='13121C'",outFields="FLD_ZONE,ZONE_SUBTY,SFHA_TF,SOURCE_CIT",geometry=f"{lo0},{la0},{lo1},{la1}",geometryType="esriGeometryEnvelope",inSR=4326,outSR=4326,spatialRel="esriSpatialRelIntersects",resultOffset=off,resultRecordCount=500),timeout=120).json()
            if "features" in j: break
        except Exception: pass
        time.sleep(4)
    feats+=j["features"]
    if len(j["features"])<500: break
    off+=500
cur=gpd.GeoDataFrame.from_features(feats,crs=4326).to_crs(2240); cur["geometry"]=cur.geometry.make_valid()
sub=cur.ZONE_SUBTY.fillna("").str.upper()
cur100=unary_union(cur[cur.SFHA_TF=="T"].geometry)
cur500=unary_union(cur[(cur.FLD_ZONE=="X")&sub.str.contains("0.2 PCT")].geometry)
print("current zones fetched:",len(cur), "citations:",cur.SOURCE_CIT.value_counts().head(6).to_dict())
# alignment check outside the revised area, within the PDF's extent
from shapely.geometry import box
win=box(*new100.buffer(-0).bounds).intersection(box(*cur100.bounds)).difference(rev.buffer(150))
def iou(dx,dy):
    a=affinity.translate(new100,dx,dy).intersection(win); b=cur100.intersection(win)
    u=a.union(b).area; return a.intersection(b).area/u if u else 0
base=iou(0,0); best=(base,0,0)
for dx in range(-150,151,25):
    for dy in range(-150,151,25):
        v=iou(dx,dy)
        if v>best[0]: best=(v,dx,dy)
print(f"agreement with current map outside revised area (IoU): as placed {base:.3f}; best {best[0]:.3f} at shift dx={best[1]} ft, dy={best[2]} ft")
dx,dy=(best[1],best[2]) if best[0]-base>0.02 else (0,0)
n100=affinity.translate(new100,dx,dy); n500=affinity.translate(new500,dx,dy); R=affinity.translate(rev,dx,dy)
c100=cur100.intersection(R); c500=cur500.intersection(R); p100=n100.intersection(R); p500=n500.intersection(R)
res={"newly_100yr_acres":p100.difference(c100).area/AC,
     "newly_100yr_was_500yr_acres":p100.intersection(c500).difference(c100).area/AC,
     "newly_500yr_acres":p500.difference(c100.union(c500)).area/AC,
     "removed_from_100yr_acres":c100.difference(p100).area/AC,
     "stays_100yr_acres":c100.intersection(p100).area/AC,
     "current_100yr_in_revised_area":c100.area/AC,"pending_100yr_in_revised_area":p100.area/AC,"revised_area_acres":R.area/AC}
print(json.dumps({k:round(v,2) for k,v in res.items()},indent=1))
geoms=[("newly_100yr",p100.difference(c100)),("removed_from_100yr",c100.difference(p100)),("stays_100yr",c100.intersection(p100)),("newly_500yr",p500.difference(c100.union(c500))),("revised_area",R),("current_100yr",c100),("pending_100yr",p100)]
gpd.GeoDataFrame({"change":[g[0] for g in geoms],"status":"pending","case_no":"26-04-1523P","eff_date":"2027-03-11","acres":[g[1].area/AC for g in geoms]},geometry=[g[1] for g in geoms],crs=2240).to_crs(4326).to_file(f"{D}/pending_vs_current.gpkg",driver="GPKG")
json.dump({k:round(v,3) for k,v in res.items()},open(f"{D}/pending_vs_current.json","w"))
