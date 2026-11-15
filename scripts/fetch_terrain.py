"""Usage: EE_PROJECT=<your-gcp-project> python scripts/fetch_terrain.py run data/raw/global/terrain/gmted_terrain_0p05.npz
Global 0.05 deg terrain layers from GMTED2010 (7.5 arc-sec, ~230 m) via Earth Engine.
Per 0.01 deg (~1.1 km) window: median slope, p95 slope, relief (p98-p02 elevation) -- the Phase 1 definitions.
Per 0.05 deg cell: the MIN and MAX of those 1 km values, so the stage-1 envelope stays recall-preserving."""
import os, ee, io, sys, time, numpy as np, requests
from concurrent.futures import ThreadPoolExecutor
ee.Initialize(project=os.environ["EE_PROJECT"])  # your own Google Cloud project with Earth Engine enabled
dem = ee.Image("USGS/GMTED2010_FULL").select("be75")
slope = ee.Terrain.slope(dem)
p1 = ee.Projection("EPSG:4326").atScale(1113.2)   # 0.01 deg
p5 = ee.Projection("EPSG:4326").atScale(5565.97)  # 0.05 deg
def win(img, red, name):
    return img.reduceResolution(reducer=red, maxPixels=1024).reproject(p1).rename(name)
w = ee.Image.cat([win(slope, ee.Reducer.median(), "s50"), win(slope, ee.Reducer.percentile([95]), "s95"),
                  win(dem, ee.Reducer.percentile([98]), "e98"), win(dem, ee.Reducer.percentile([2]), "e02")])
w = w.addBands(w.select("e98").subtract(w.select("e02")).rename("rel")).select(["s50", "s95", "rel"])
def mm(img, red): return img.reduceResolution(reducer=red, maxPixels=64).reproject(p5)
lo = mm(w, ee.Reducer.min()); hi = mm(w, ee.Reducer.max())
img = lo.rename(["s50_lo", "s95_lo", "rel_lo"]).addBands(hi.rename(["s50_hi", "s95_hi", "rel_hi"]))
NAMES = ["s50_lo", "s95_lo", "rel_lo", "s50_hi", "s95_hi", "rel_hi"]
def tile(lon0, lat1, deg=10):
    n = int(deg / 0.05)
    url = img.getDownloadURL({"region": ee.Geometry.Rectangle([lon0, lat1 - deg, lon0 + deg, lat1], geodesic=False), "crs": "EPSG:4326",
                              "crsTransform": [0.05, 0, lon0, 0, -0.05, lat1], "dimensions": f"{n}x{n}", "format": "NPY"})
    for k in range(4):
        try:
            a = np.load(io.BytesIO(requests.get(url, timeout=600).content)); return np.stack([a[x] for x in NAMES]).astype(np.float32)
        except Exception as e:
            err = e; time.sleep(5 * (k + 1))
    raise err
if __name__ == "__main__":
    if sys.argv[1] == "test":
        t = time.time(); a = tile(-120, 40); print(a.shape, time.time() - t, [float(np.nanmedian(a[i])) for i in range(6)], np.isnan(a).mean())

def run(out):
    from pathlib import Path
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    # GMTED2010 covers 56 S to 84 N. Rows are 0.05 deg from 90 N; fill outside coverage with NaN.
    G = np.full((6, 3600, 7200), np.nan, dtype=np.float32)
    D = 5; jobs = [(lon0, lat1) for lat1 in range(84, -56, -D) for lon0 in range(-180, 180, D)]
    done = 0; t0 = time.time()
    with ThreadPoolExecutor(6) as ex:
        for (lon0, lat1), a in zip(jobs, ex.map(lambda j: tile(*j, D), jobs)):
            r0, c0 = int(round((90 - lat1) / 0.05)), int(round((lon0 + 180) / 0.05)); n = int(D / 0.05)
            G[:, r0:r0 + n, c0:c0 + n] = a; done += 1
            if done % 100 == 0: print(f"{done}/{len(jobs)} {time.time()-t0:.0f}s", flush=True)
    np.savez_compressed(out, **dict(zip(NAMES, G)), lon_west=-180.0, lat_north=90.0, d=0.05)
    print("saved", out, "valid share", float(np.isfinite(G[0]).mean()), flush=True)
if __name__ == "__main__" and sys.argv[1] == "run": run(sys.argv[2])
