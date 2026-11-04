"""Download the Porto drivable road network from OpenStreetMap and index it on an H3 grid.

Outputs (data/roads/):
  edges.parquet       one row per undirected road segment: id, road class, name, max speed, length, WKT geometry
  road_cells.parquet  (h3 cell, edge_id, ring) pairs: every resolution-12 cell within ~30 m of a road,
                      so a GPS fix can be matched to nearby roads with an equality join instead of a
                      geometric search

usage: python prepare_roads.py
"""
import pathlib

import h3
import numpy as np
import osmnx as ox
import pandas as pd

OUT = pathlib.Path(__file__).parent / "data" / "roads"
BBOX = dict(north=41.25, south=41.10, east=-8.50, west=-8.70)  # Porto, Matosinhos, Gaia
RES = 12            # H3 resolution 12: cells about 9 m across
RING = 2            # neighbours 2 rings out: about 30 m either side of the road
STEP_M = 5          # sample each road every 5 m


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    g = ox.graph_from_bbox(bbox=(BBOX["north"], BBOX["south"], BBOX["east"], BBOX["west"]), network_type="drive")
    edges = ox.graph_to_gdfs(ox.convert.to_undirected(g), nodes=False).reset_index()
    edges["edge_id"] = np.arange(len(edges), dtype=np.int64)
    first = lambda v: v[0] if isinstance(v, list) else v
    edges["highway"] = edges["highway"].map(first).astype(str)
    edges["name"] = edges.get("name", pd.Series(index=edges.index)).map(first).astype("string")
    edges["maxspeed"] = edges.get("maxspeed", pd.Series(index=edges.index)).map(first).astype("string")

    # sample points along each road in metres, then index them on the H3 grid
    utm = edges.to_crs(edges.estimate_utm_crs())
    rows = []
    for eid, line in zip(utm.edge_id, utm.geometry):
        n = max(int(line.length // STEP_M), 1)
        pts = [line.interpolate(i * line.length / n) for i in range(n + 1)]
        rows.extend((eid, p.x, p.y) for p in pts)
    pts = pd.DataFrame(rows, columns=["edge_id", "x", "y"])
    import geopandas as gpd
    ll = gpd.GeoSeries(gpd.points_from_xy(pts.x, pts.y), crs=utm.crs).to_crs(4326)
    pts["cell"] = [h3.latlng_to_cell(p.y, p.x, RES) for p in ll]
    core = pts[["cell", "edge_id"]].drop_duplicates()
    ring = [(c, e, k) for c0, e in core.itertuples(index=False) for k in range(RING + 1)
            for c in (h3.grid_ring(c0, k) if k else [c0])]
    cells = (pd.DataFrame(ring, columns=["cell", "edge_id", "ring"])
             .groupby(["cell", "edge_id"], as_index=False)["ring"].min())
    # distance in metres from each cell's centre to the road, so a fix picks the truly nearest road
    import shapely
    centres = np.array([h3.cell_to_latlng(c) for c in cells["cell"]])
    pc = gpd.GeoSeries(gpd.points_from_xy(centres[:, 1], centres[:, 0]), crs=4326).to_crs(utm.crs)
    lines = utm.set_index("edge_id").geometry.loc[cells["edge_id"]].values
    cells["dist_m"] = shapely.distance(pc.values, lines).astype("float32").round(1)
    cells["cell"] = cells["cell"].map(lambda c: int(c, 16)).astype("int64")

    keep = ["edge_id", "u", "v", "highway", "name", "maxspeed", "length"]
    out = edges[keep].rename(columns={"length": "length_m"}).copy()
    out["wkt"] = edges.geometry.to_wkt()
    out.to_parquet(OUT / "edges.parquet", index=False)
    cells.to_parquet(OUT / "road_cells.parquet", index=False)
    print(f"{len(out):,} road segments, {out.length_m.sum() / 1000:,.0f} km; "
          f"{len(cells):,} (cell, segment) pairs at H3 resolution {RES}")
    print(out.groupby("highway").length_m.sum().div(1000).round(0).sort_values(ascending=False).head(10).to_string())


if __name__ == "__main__":
    main()
