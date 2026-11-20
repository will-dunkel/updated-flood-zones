import sys, json, time, requests, fitz, warnings
import geopandas as gpd
from shapely.geometry import Polygon, LineString, box
from shapely.ops import unary_union, polygonize
from shapely import affinity
from pyproj import Transformer
warnings.filterwarnings("ignore")
D=sys.argv[1]; doc=fitz.open(f"{D}/lomr.pdf"); p=doc[11]
S=6000/72/12                     # ft per PDF point (1:6,000)
TX,TY,TE,TN=600.5,200.7,2225000.0,1330000.0   # tick positions -> GA West (EPSG:2240) ft
def to_sp(x,y): return (TE+(x-TX)*S, TN-(y-TY)*S)
def subpaths(items):
    rings=[]; cur=[]
    for it in items:
        k=it[0]
        if k=="l": a,b=it[1],it[2]; seg=[(a.x,a.y),(b.x,b.y)]
        elif k=="c": a,b,c,dd=it[1],it[2],it[3],it[4]; seg=[(a.x,a.y)]+[(((1-t)**3)*a.x+3*((1-t)**2)*t*b.x+3*(1-t)*t*t*c.x+t**3*dd.x,((1-t)**3)*a.y+3*((1-t)**2)*t*b.y+3*(1-t)*t*t*c.y+t**3*dd.y) for t in (0.25,0.5,0.75,1)]
        elif k=="re": r=it[1]; rings.append([(r.x0,r.y0),(r.x1,r.y0),(r.x1,r.y1),(r.x0,r.y1)]); continue
        elif k=="qu": q=it[1]; rings.append([(q.ul.x,q.ul.y),(q.ur.x,q.ur.y),(q.lr.x,q.lr.y),(q.ll.x,q.ll.y)]); continue
        else: continue
        if cur and (abs(cur[-1][0]-seg[0][0])>0.05 or abs(cur[-1][1]-seg[0][1])>0.05):
            rings.append(cur); cur=[]
        cur = cur+seg[1:] if cur else seg
    if cur: rings.append(cur)
    return [r for r in rings if len(r)>=3]
def fill_geom(dr, evenodd=True):
    g=None
    for r in subpaths(dr["items"]):
        poly=Polygon([to_sp(*pt) for pt in r]).buffer(0)
        if poly.is_empty: continue
        g=poly if g is None else (g.symmetric_difference(poly) if evenodd else g.union(poly))
    return g
cy,org=[],[]; revised=None
for dr in p.get_drawings():
    f=tuple(round(v,2) for v in (dr.get("fill") or ()))
    if f==(0.0,0.9,1.0):
        g=fill_geom(dr, dr.get("even_odd", True)); 
        if g is not None: cy.append(g)
    elif f==(1.0,0.5,0.0):
        g=fill_geom(dr, dr.get("even_odd", True))
        if g is not None: org.append(g)
    elif dr.get("fill") is None and round(dr.get("width") or 0,1)==2.5:
        r=dr["rect"]
        if 330<r.x0<340 and 120<r.y0<125:   # the REVISED AREA outline on this panel
            pts=[]; [pts.extend([to_sp(it[1].x,it[1].y),to_sp(it[2].x,it[2].y)]) for it in dr["items"] if it[0]=="l"]
            lines=[LineString([to_sp(it[1].x,it[1].y),to_sp(it[2].x,it[2].y)]) for it in dr["items"] if it[0]=="l"]
            revised=list(polygonize(unary_union(lines)))
            revised=unary_union(revised) if revised else Polygon(pts).convex_hull
new100=unary_union(cy); new500=unary_union(org).difference(new100)
crs="EPSG:2240"
out=gpd.GeoDataFrame({"layer":["new_100yr","new_500yr","revised_area"]},geometry=[new100,new500,revised],crs=crs).to_crs(4326)
out.to_file(f"{D}/lomr_extracted.gpkg",driver="GPKG")
print("revised area acres:", round(revised.area/43560,2), "| extracted SFHA total acres:", round(new100.area/43560,1), "| 0.2% acres:", round(new500.area/43560,1))
print("revised area bounds (lon/lat):", [round(v,5) for v in out.geometry.iloc[2].bounds])
