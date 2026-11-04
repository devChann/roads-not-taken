"""Validate the road matching on synthetic drives where the true route is known.

1. Drive random routes along real Porto roads (no U-turns), sampling a GPS fix every 15 s
   at a city speed, and record which road segment each fix truly came from.
2. Add realistic GPS noise (about 6 m) and inject occasional large jumps (300 m to 2 km).
3. Write the drives in the Porto CSV format, run the same Spark pipeline on them, and score:
   - match accuracy: share of fixes matched to the true segment
   - near-correct: matched to a segment touching the true one (fixes at junctions are ambiguous)
   - jump removal: share of injected jumps the cleaning step removed

usage: python validate.py [--trips 2000]
"""
import argparse
import json
import pathlib
import random
import subprocess
import sys

import numpy as np
import pandas as pd
from shapely import wkt
from shapely.geometry import LineString

HERE = pathlib.Path(__file__).parent
ROADS = HERE / "data" / "roads"
VAL = HERE / "data" / "validation"
M_LAT = 111_320.0
M_LON = 111_320.0 * np.cos(np.radians(41.15))


def oriented(coords, start):
    """Return coords running away from `start` (the end of the previous segment)."""
    a, b = np.array(coords[0]), np.array(coords[-1])
    return coords if np.hypot(*(a - start)) <= np.hypot(*(b - start)) else coords[::-1]


def drive(edges, by_node, rng, target_m):
    e = edges.sample(1, random_state=rng.integers(1e9)).iloc[0]
    node, coords, ids = e.v, list(e.geom.coords), [e.edge_id] * len(e.geom.coords)
    path, seen = [(e.edge_id, list(e.geom.coords))], {e.edge_id}
    total = e.length_m
    prev = e.edge_id
    steps = 0
    while total < target_m and steps < 400:
        steps += 1
        nxt = [(eid, other) for eid, other in by_node.get(node, []) if eid != prev]
        options = [o for o in nxt if o[0] not in seen] or nxt
        if not options:
            break
        eid, other = options[rng.integers(len(options))]
        row = edges.loc[eid]
        c = oriented(list(row.geom.coords), np.array(path[-1][1][-1]))
        path.append((eid, c))
        seen.add(eid)
        total += row.length_m
        node, prev = other, eid
    # resample along the route every speed * 15 s, remembering the true segment
    pts, seg = [], []
    for eid, c in path:
        for xy in (c if not pts else c[1:]):
            pts.append(xy)
            seg.append(eid)
    pts = np.array(pts)
    xy_m = np.c_[pts[:, 0] * M_LON, pts[:, 1] * M_LAT]
    d = np.r_[0, np.cumsum(np.hypot(*np.diff(xy_m, axis=0).T))]
    step = rng.uniform(6, 13) * 15        # 22-47 km/h city driving, a fix every 15 s
    s = np.arange(0, d[-1], step)
    lon = np.interp(s, d, pts[:, 0])
    lat = np.interp(s, d, pts[:, 1])
    k = np.clip(np.searchsorted(d, s, side="right") - 1, 0, len(seg) - 1)
    return lat, lon, np.array(seg)[k]


def main(n_trips):
    VAL.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    edges = pd.read_parquet(ROADS / "edges.parquet")
    edges = edges[edges.highway.isin(["motorway", "trunk", "primary", "secondary", "tertiary", "residential",
                                      "unclassified", "motorway_link", "primary_link", "secondary_link",
                                      "tertiary_link"])].copy()
    edges["geom"] = edges.wkt.map(wkt.loads)
    edges = edges.set_index("edge_id", drop=False)
    by_node = {}
    for eid, u, v in zip(edges.edge_id, edges.u, edges.v):
        by_node.setdefault(u, []).append((eid, v))
        by_node.setdefault(v, []).append((eid, u))

    rows, truth = [], []
    for t in range(n_trips):
        lat, lon, seg = drive(edges, by_node, rng, rng.uniform(2000, 8000))
        if len(lat) < 4:
            continue
        noise = rng.normal(0, 6, (len(lat), 2))
        jump = rng.random(len(lat)) < 0.01
        jump[0] = False
        r, a = rng.uniform(300, 2000, len(lat)), rng.uniform(0, 2 * np.pi, len(lat))
        noise[jump] += np.c_[r * np.cos(a), r * np.sin(a)][jump]
        la = lat + noise[:, 1] / M_LAT
        lo = lon + noise[:, 0] / M_LON
        tid = f"SYN{t:05d}"
        poly = json.dumps([[round(x, 6), round(y, 6)] for x, y in zip(lo, la)])
        rows.append({"TRIP_ID": tid, "CALL_TYPE": "C", "ORIGIN_CALL": "", "ORIGIN_STAND": "",
                     "TAXI_ID": 99000000 + t % 50, "TIMESTAMP": 1372636800 + t * 600, "DAY_TYPE": "A",
                     "MISSING_DATA": "False", "POLYLINE": poly})
        truth.append(pd.DataFrame({"trip_id": tid, "i": np.arange(len(lat)), "true_edge": seg, "jump": jump}))
    pd.DataFrame(rows).to_csv(VAL / "synthetic_trips.csv", index=False)
    truth = pd.concat(truth, ignore_index=True)
    truth.to_parquet(VAL / "truth.parquet", index=False)
    print(f"{len(rows):,} synthetic trips, {len(truth):,} fixes, {int(truth.jump.sum())} injected jumps")

    subprocess.run([sys.executable, str(HERE / "pipeline.py"), "--input", str(VAL / "synthetic_trips.csv"),
                    "--out", str(VAL / "out"), "--save-matches", "--broadcast"], check=True)

    m = pd.read_parquet(VAL / "out" / "matches.parquet")
    flags = pd.read_parquet(VAL / "out" / "fix_flags.parquet")
    allroads = pd.read_parquet(ROADS / "edges.parquet")[["edge_id", "u", "v", "name"]]
    ends = dict(zip(allroads.edge_id, zip(allroads.u, allroads.v)))
    names = dict(zip(allroads.edge_id, allroads.name))
    j = truth.merge(flags, on=["trip_id", "i"], how="left").merge(m, on=["trip_id", "i"], how="left")
    real = j[~j.jump & j.ok.fillna(False)]
    matched = real[real.edge_id.notna()]
    exact = (matched.edge_id == matched.true_edge)
    touch = matched.apply(lambda r: bool(set(ends.get(int(r.edge_id), ())) & set(ends.get(int(r.true_edge), ()))), axis=1)
    res = {
        "synthetic_trips": len(rows), "fixes": len(truth),
        "jumps_injected": int(truth.jump.sum()),
        "jumps_removed_pct": round(100 * (~j[j.jump].ok.fillna(True)).mean(), 1),
        "good_fixes_wrongly_dropped_pct": round(100 * (~j[~j.jump].ok.fillna(True)).mean(), 2),
        "good_fixes_matched_pct": round(100 * len(matched) / max(len(real), 1), 1),
        "match_exact_pct": round(100 * exact.mean(), 1),
        "match_exact_or_adjacent_pct": round(100 * (exact | touch).mean(), 1),
        "match_correct_street_pct": round(100 * (exact | touch | matched.apply(
            lambda r: pd.notna(names.get(int(r.true_edge))) and names.get(int(r.true_edge)) == names.get(int(r.edge_id)),
            axis=1)).mean(), 1),
    }
    (VAL / "validation.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trips", type=int, default=2000)
    main(ap.parse_args().trips)
