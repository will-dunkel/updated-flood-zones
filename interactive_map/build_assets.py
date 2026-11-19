import sys, io, json, math, gzip, time, requests, numpy as np, pandas as pd, geopandas as gpd, warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image
from rasterio.features import rasterize
from rasterio.transform import from_origin
warnings.filterwarnings("ignore")
W=Path(sys.argv[1]); SITE=W/"site"; TC=W/"tilecache"
R=6378137.0; HALF=math.pi*R
BASE="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"
REF="https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}"
g=gpd.read_file("data/projected/TX_Harris_48201/newly_added_zones.gpkg").to_crs(3857)
x0,y0,x1,y1=g.total_bounds; pad=2500; x0-=pad;y0-=pad;x1+=pad;y1+=pad
def tile(u,z,x,y,kind):
    f=TC/f"{kind}_{z}_{x}_{y}.img"
    if f.exists(): return f.read_bytes()
    for i in range(8):
        try:
            r=requests.get(u.format(z=z,x=x,y=y),timeout=60)
            if r.status_code==200: f.write_bytes(r.content); return r.content
            if r.status_code==404: return b""
        except Exception: pass
        time.sleep(2+i*2)
    return b""
def merc2ll(x,y): return (math.degrees(2*math.atan(math.exp(y/R))-math.pi/2), math.degrees(x/R))
CODES={"newly_100yr_from_none":1,"newly_100yr_from_500yr":2,"newly_500yr":3}
order=["newly_500yr","newly_100yr_from_500yr","newly_100yr_from_none"]
pal=[0,0,0, 192,57,43, 230,126,34, 241,196,15]+[0]*(256*3-12)
alpha=bytes([0,200,200,170]+[0]*252)
manifest={}
for z,chunk in ((12,None),(14,8)):
    m=2*HALF/2**z
    tx0=int((x0+HALF)//m); tx1=int((x1+HALF)//m); ty0=int((HALF-y1)//m); ty1=int((HALF-y0)//m)
    if chunk: tx0-=tx0%chunk; ty0-=ty0%chunk
    else: chunk=max(tx1-tx0+1,ty1-ty0+1)
    jobs=[(u,z,x,y,k) for x in range(tx0,tx1+1) for y in range(ty0,ty1+1) for u,k in ((BASE,"b"),(REF,"r"))]
    with ThreadPoolExecutor(16) as ex: list(ex.map(lambda a: tile(*a), jobs))
    print("z",z,"tiles",len(jobs)//2, flush=True)
    out=[]
    for cx in range(tx0,tx1+1,chunk):
        for cy in range(ty0,ty1+1,chunk):
            nx=min(chunk,tx1+1-cx); ny=min(chunk,ty1+1-cy)
            img=Image.new("RGB",(nx*256,ny*256),(232,232,232))
            for i in range(nx):
                for j in range(ny):
                    for k in ("b","r"):
                        b=tile(BASE if k=="b" else REF,z,cx+i,cy+j,k)
                        if not b: continue
                        t=Image.open(io.BytesIO(b)).convert("RGBA")
                        img.paste(t,(i*256,j*256),t)
            mx0=-HALF+cx*m; my1=HALF-cy*m; mx1=mx0+nx*m; my0=my1-ny*m
            # flood overlay at 2x the base resolution
            s=2; res=m/256/s; H,Wd=ny*256*s,nx*256*s
            sub=g.cx[mx0:mx1,my0:my1]
            arr=np.zeros((H,Wd),np.uint8)
            tr=from_origin(mx0,my1,res,res)
            for c in order:
                gg=sub[sub.change==c].geometry
                if len(gg): rasterize(((geo,CODES[c]) for geo in gg),out=arr,transform=tr)
            name=f"z{z}_{cx}_{cy}"
            img.save(SITE/"tiles"/f"{name}.jpg",quality=72,optimize=True,progressive=True)
            has_flood=bool(arr.any())
            if has_flood:
                p=Image.fromarray(arr,"P"); p.putpalette(pal); p.save(SITE/"tiles"/f"{name}_f.png",transparency=alpha,optimize=True)
            (s_lat,w_lon)=merc2ll(mx0,my0); (n_lat,e_lon)=merc2ll(mx1,my1)
            out.append(dict(n=name,b=[[round(s_lat,6),round(w_lon,6)],[round(n_lat,6),round(e_lon,6)]],f=has_flood))
    manifest[f"z{z}"]=out
    print("z",z,"chunks",len(out),flush=True)
# parcels
p=pd.read_csv("data/projected/TX_Harris_48201/parcels_projected_changes.csv.gz",dtype=str)
pc={"newly_100yr_from_none":1,"newly_100yr_from_500yr":2,"newly_500yr":3,"removed_from_100yr":4}
p=p[p.change.isin(pc)&p.lat.notna()]
rows=[[round(float(a),5),round(float(b),5),pc[c],ad if isinstance(ad,str) else "",z if isinstance(z,str) else "",i,(ct.title() if isinstance(ct,str) else "")]
      for a,b,c,ad,z,i,ct in zip(p.lat,p.lon,p.change,p.address,p.site_zip,p.LOWPARCELID,p.site_city)]
raw=json.dumps(rows,separators=(",",":")).encode()
(SITE/"parcels.json.gz").write_bytes(gzip.compress(raw,9))
print("parcels",len(rows),"raw MB",round(len(raw)/1e6,1),"gz MB",round((SITE/"parcels.json.gz").stat().st_size/1e6,1))
# zip summary
zs=p[p.change.str.startswith("newly_100")].groupby("site_zip").size().sort_values(ascending=False)
zc=p.assign(lat=p.lat.astype(float),lon=p.lon.astype(float)).groupby("site_zip")[["lat","lon"]].median()
manifest["zips"]=[dict(zip=k,n=int(v),lat=round(zc.loc[k,"lat"],5),lon=round(zc.loc[k,"lon"],5)) for k,v in zs.head(12).items()]
manifest["bounds"]=[list(merc2ll(x0,y0)),list(merc2ll(x1,y1))]
(SITE/"manifest.json").write_text(json.dumps(manifest,separators=(",",":")))
print("done")
