"""Export what the web page needs to docs/data (GitHub Pages serves docs/).

  roads.geojson  every road segment with its trips, taxis and days, simplified to about 2 m
  summary.json   headline numbers, coverage by road class, join timings and validation results

usage: python export_web.py   (after pipeline.py, the shuffle run and validate.py)
"""
import json
import pathlib

import pandas as pd
import shapely
from shapely import wkt

HERE = pathlib.Path(__file__).parent
D = HERE / "data"
OUT = HERE / "docs" / "data"
GROUP = {"motorway": "m", "motorway_link": "m", "trunk": "m", "trunk_link": "m",
         "primary": "p", "primary_link": "p", "secondary": "s", "secondary_link": "s",
         "tertiary": "t", "tertiary_link": "t", "residential": "r"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    edges = pd.read_parquet(D / "roads" / "edges.parquet")
    cov = pd.read_parquet(D / "out" / "coverage.parquet")
    e = edges.merge(cov, on="edge_id", how="left")
    for c in ("trips", "taxis", "days"):
        e[c] = e[c].fillna(0).astype(int)
    geoms = shapely.simplify(shapely.from_wkt(e.wkt.values), 0.00002, preserve_topology=False)
    feats = []
    for row, g in zip(e.itertuples(index=False), geoms):
        coords = [[round(x, 5), round(y, 5)] for x, y in g.coords]
        props = {"t": row.trips, "x": row.taxis, "d": row.days, "c": GROUP.get(row.highway, "o"),
                 "h": row.highway, "l": round(row.length_m)}
        if isinstance(row.name, str) and row.name:
            props["n"] = row.name
        feats.append({"type": "Feature", "properties": props, "geometry": {"type": "LineString", "coordinates": coords}})
    (OUT / "roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                                  separators=(",", ":"), ensure_ascii=False))

    s = json.loads((D / "out" / "summary.json").read_text())
    shuffle = json.loads((D / "out_shuffle" / "summary.json").read_text())
    val = json.loads((D / "validation" / "validation.json").read_text())
    by_class = pd.read_csv(D / "out" / "coverage_by_class.csv")
    by_class["group"] = by_class.highway.map(lambda h: GROUP.get(h, "o"))
    groups = (by_class.groupby("group")[["km", "km_driven", "km_10plus_trips"]].sum()
              .reindex(["m", "p", "s", "t", "r", "o"]).reset_index())
    hours = pd.read_csv(D / "out" / "fixes_by_hour.csv")
    summary = {
        "run": s,
        "join": {
            "broadcast": {"match_s": round(s["seconds_match"] - s["seconds_explode_and_clean"], 1), "total_s": s["seconds_coverage"]},
            "shuffle": {"match_s": round(shuffle["seconds_match"] - shuffle["seconds_explode_and_clean"], 1), "total_s": shuffle["seconds_coverage"]},
            "matched_fixes_both": s["fixes_matched_to_a_road"] == shuffle["fixes_matched_to_a_road"],
        },
        "validation": val,
        "classes": [{"group": r.group, "km": round(r.km, 1), "driven": round(r.km_driven, 1),
                     "regular": round(r.km_10plus_trips, 1)} for r in groups.itertuples()],
        "hours": hours.to_dict("records"),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1))
    mb = (OUT / "roads.geojson").stat().st_size / 1e6
    print(f"roads.geojson {mb:.1f} MB, {len(feats):,} segments; summary.json written")


if __name__ == "__main__":
    main()
