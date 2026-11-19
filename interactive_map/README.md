# Harris County interactive map (projected)

`index.html` is the published page. `build_assets.py` builds everything it loads:
basemap mosaics (Esri light gray, zoom levels 12 and 14), flood-change overlays
rasterized from `data/projected/TX_Harris_48201/newly_added_zones.gpkg`, a
manifest, and the parcel points.

Before running the build, decompress `newly_added_zones.gpkg.gz` (the build
needs the uncompressed `.gpkg`). Run the build from the repo root:

```bash
python3 interactive_map/build_assets.py <work_dir>
```

The build writes the gzipped parcel file `parcels.json.gz` into
`<work_dir>/site/`. The published page instead loads it as three plain-JSON
files, `parcels_0.json` through `parcels_2.json`, because artifacts can't
serve gzip. Split it the same way before publishing.

Artifact pages cannot load outside map tiles. That is why the basemap is
pre-rendered and shipped with the page.
