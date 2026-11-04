"""Draw the coverage map: roads coloured by how many trips drove them, undriven roads in grey.

usage: python make_map.py   (after pipeline.py) -> docs/coverage.png
"""
import pathlib

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LogNorm
from shapely import wkt

HERE = pathlib.Path(__file__).parent
edges = pd.read_parquet(HERE / "data" / "roads" / "edges.parquet")
cov = pd.read_parquet(HERE / "data" / "out" / "coverage.parquet")
g = gpd.GeoDataFrame(edges.merge(cov, on="edge_id", how="left"), geometry=edges.wkt.map(wkt.loads), crs=4326)
g["trips"] = g.trips.fillna(0)

fig, ax = plt.subplots(figsize=(11, 11), dpi=150)
g[g.trips == 0].plot(ax=ax, color="#c9c9c9", linewidth=0.35)
d = g[g.trips > 0].sort_values("trips")
d.plot(ax=ax, column="trips", cmap="plasma", norm=LogNorm(vmin=1, vmax=max(d.trips.max(), 10)),
       linewidth=np.clip(np.log10(d.trips + 1) * 0.6, 0.3, 2.2), legend=True,
       legend_kwds={"label": "trips that drove the segment (log scale)", "shrink": 0.6})
km, km_d = g.length_m.sum() / 1000, g.length_m[g.trips > 0].sum() / 1000
ax.set_title(f"Every street, or just the busy ones?\nPorto taxi fleet: {km_d:,.0f} of {km:,.0f} km of drivable road driven ({100 * km_d / km:.0f}%). "
             "Grey = never driven.", fontsize=11)
ax.set_axis_off()
(HERE / "docs").mkdir(exist_ok=True)
fig.savefig(HERE / "docs" / "coverage.png", bbox_inches="tight", facecolor="white")
print("wrote docs/coverage.png")
