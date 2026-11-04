"""Fleet road coverage: which roads has the fleet driven, how often, and what's missing?

PySpark pipeline over the Porto taxi dataset (442 taxis, 1.7 million trips, a GPS fix every 15 s):
  1. explode each trip's polyline into timestamped GPS fixes
  2. clean: duplicate trips, points outside the city, impossible jumps between consecutive fixes
  3. index fixes on the H3 grid and match each one to the nearest road with an equality join
  4. aggregate coverage per road segment and per road class
Outputs go to data/out/.

usage: python pipeline.py [--sample 0.05] [--broadcast]
"""
import argparse
import json
import os
import pathlib
import sys
import time

os.environ.setdefault("PYSPARK_PYTHON", sys.executable)   # Spark's Python workers use this project's environment

import h3
import pandas as pd
from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import LongType

HERE = pathlib.Path(__file__).parent
RAW = HERE / "data" / "raw" / "train.csv"
ROADS = HERE / "data" / "roads"
OUT = HERE / "data" / "out"
BBOX = dict(north=41.25, south=41.10, east=-8.50, west=-8.70)
RES = 12
MAX_SPEED_MS = 40.0   # 144 km/h between consecutive fixes = a GPS jump, not a taxi
STEP_S = 15           # the dataset samples every 15 seconds


@F.pandas_udf(LongType())
def h3_cell(lat: pd.Series, lon: pd.Series) -> pd.Series:
    return pd.Series([int(h3.latlng_to_cell(a, b, RES), 16) for a, b in zip(lat, lon)], dtype="int64")


def hav(lat1, lon1, lat2, lon2):
    p1, p2 = F.radians(lat1), F.radians(lat2)
    a = F.sin((p2 - p1) / 2) ** 2 + F.cos(p1) * F.cos(p2) * F.sin(F.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * F.asin(F.sqrt(a))


def main(sample, broadcast, raw_path=RAW, out=OUT, save_matches=False):
    global OUT
    OUT = pathlib.Path(out)
    spark = (SparkSession.builder.appName("fleet-road-coverage")
             .config("spark.driver.memory", "10g")
             .config("spark.sql.shuffle.partitions", "64")
             .config("spark.sql.adaptive.enabled", "true")
             .config("spark.sql.adaptive.skewJoin.enabled", "true")
             .config("spark.sql.execution.arrow.pyspark.enabled", "true")
             .getOrCreate())
    spark.sparkContext.setLogLevel("WARN")
    if not broadcast:   # force a shuffle (sort-merge) join so the two strategies can be compared
        spark.conf.set("spark.sql.autoBroadcastJoinThreshold", "-1")
    OUT.mkdir(parents=True, exist_ok=True)
    stats, t0 = {}, time.time()

    def lap(name):
        stats[f"seconds_{name}"] = round(time.time() - t0, 1)

    # 1. trips -> fixes
    raw = spark.read.option("header", True).csv(str(raw_path))
    trips = raw.select(F.col("TRIP_ID").alias("trip_id"), F.col("TAXI_ID").cast("long").alias("taxi_id"),
                       F.col("TIMESTAMP").cast("long").alias("start_s"),
                       (F.col("MISSING_DATA") == "True").alias("missing"), F.col("POLYLINE").alias("polyline"))
    if sample:
        trips = trips.sample(fraction=sample, seed=7)
    stats["trips_raw"] = trips.count()
    trips = trips.dropDuplicates(["trip_id"])
    r = trips.agg(F.count("*").alias("n"), F.countDistinct("taxi_id").alias("taxis"),
                  F.min("start_s").alias("t0"), F.max("start_s").alias("t1")).first()
    stats.update(trips_after_dedup=r["n"], taxis=r["taxis"],
                 first_trip_utc=time.strftime("%Y-%m-%d", time.gmtime(r["t0"])),
                 last_trip_utc=time.strftime("%Y-%m-%d", time.gmtime(r["t1"])))
    lap("read")

    fixes = (trips
             .select("trip_id", "taxi_id", "start_s", F.posexplode(F.from_json("polyline", "array<array<double>>")).alias("i", "p"))
             .select("trip_id", "taxi_id", "i",
                     F.timestamp_seconds(F.col("start_s") + STEP_S * F.col("i")).alias("ts"),
                     F.col("p")[1].alias("lat"), F.col("p")[0].alias("lon")))

    # 2. clean: inside the city, and no impossible jump from the previous fix of the same trip
    inside = (F.col("lat").between(BBOX["south"], BBOX["north"]) & F.col("lon").between(BBOX["west"], BBOX["east"]))
    w = Window.partitionBy("trip_id").orderBy("i")   # shuffles fixes so each trip sits in one partition
    plat, plon, nlat, nlon = F.lag("lat").over(w), F.lag("lon").over(w), F.lead("lat").over(w), F.lead("lon").over(w)
    fixes = (fixes.withColumn("inside", inside)
             .withColumn("v_in", hav(plat, plon, F.col("lat"), F.col("lon")) / STEP_S)
             .withColumn("v_out", hav(F.col("lat"), F.col("lon"), nlat, nlon) / STEP_S)
             .withColumn("v_skip", hav(plat, plon, nlat, nlon) / (2 * STEP_S)))
    # a spike jumps away and straight back: fast in and out, while skipping it is a normal drive
    spike = ((F.col("v_in") > 15) & (F.col("v_out") > 15) & (F.col("v_skip") < 15)) | \
            ((F.col("v_in") > MAX_SPEED_MS) & (F.col("v_out") > MAX_SPEED_MS))
    fixes = fixes.withColumn("ok", F.col("inside") & ~F.coalesce(spike, F.lit(False)))
    fixes = fixes.persist()
    q = fixes.agg(F.count("*").alias("all"), F.sum(F.col("inside").cast("int")).alias("inside"),
                  F.sum(F.col("ok").cast("int")).alias("ok")).first()
    stats.update(fixes_total=q["all"], fixes_outside_city=q["all"] - q["inside"], fixes_kept=q["ok"],
                 fixes_dropped_as_jumps=q["inside"] - q["ok"])
    lap("explode_and_clean")

    # 3. match: equality join on the H3 cell, then the nearest road per fix (smallest ring)
    clean = fixes.where("ok").select("trip_id", "taxi_id", "i", "ts", "lat", "lon").withColumn("cell", h3_cell("lat", "lon"))
    cells = spark.read.parquet(str(ROADS / "road_cells.parquet"))
    if broadcast:
        cells = F.broadcast(cells)
    nearest = (clean.join(cells, "cell")
               .groupBy("trip_id", "i").agg(F.first("taxi_id").alias("taxi_id"), F.first("ts").alias("ts"),
                                             F.min_by("edge_id", "dist_m").alias("edge_id")))
    # continuity: if the fixes before and after sit on the same road, a single odd fix between them does too
    w2 = Window.partitionBy("trip_id").orderBy("i")
    prev_e, next_e = F.lag("edge_id").over(w2), F.lead("edge_id").over(w2)
    matched = nearest.withColumn("edge_id", F.when((prev_e == next_e) & (prev_e != F.col("edge_id")), prev_e)
                                 .otherwise(F.col("edge_id")))
    matched = matched.persist()
    stats["fixes_matched_to_a_road"] = matched.count()
    stats["match_rate_pct"] = round(100 * stats["fixes_matched_to_a_road"] / max(stats["fixes_kept"], 1), 1)
    lap("match")
    if save_matches:
        matched.select("trip_id", "i", "edge_id").write.mode("overwrite").parquet(str(OUT / "matches.parquet"))
        fixes.select("trip_id", "i", "ok").write.mode("overwrite").parquet(str(OUT / "fix_flags.parquet"))

    # 4. coverage per road segment
    cov = (matched.groupBy("edge_id")
           .agg(F.count("*").alias("fixes"), F.countDistinct("trip_id").alias("trips"),
                F.countDistinct("taxi_id").alias("taxis"), F.countDistinct(F.to_date("ts")).alias("days"),
                F.min("ts").alias("first_seen"), F.max("ts").alias("last_seen")))
    cov.toPandas().to_parquet(OUT / "coverage.parquet", index=False)
    hours = (matched.groupBy(F.hour("ts").alias("hour")).count().orderBy("hour").toPandas())
    lap("coverage")

    edges = pd.read_parquet(ROADS / "edges.parquet")
    c = edges.merge(pd.read_parquet(OUT / "coverage.parquet"), on="edge_id", how="left").fillna({"trips": 0})
    by_class = (c.assign(km=c.length_m / 1000, driven=c.trips > 0, regular=c.trips >= 10)
                .groupby("highway")
                .apply(lambda g: pd.Series({"km": g.km.sum(), "km_driven": g.km[g.driven].sum(),
                                            "km_10plus_trips": g.km[g.regular].sum()}), include_groups=False)
                .sort_values("km", ascending=False))
    by_class["pct_driven"] = (100 * by_class.km_driven / by_class.km).round(1)
    stats.update(road_km=round(c.length_m.sum() / 1000), road_km_driven=round(c.length_m[c.trips > 0].sum() / 1000),
                 sample=sample or 1.0, broadcast_join=broadcast)
    stats["road_km_driven_pct"] = round(100 * stats["road_km_driven"] / stats["road_km"], 1)
    by_class.round(1).to_csv(OUT / "coverage_by_class.csv")
    hours.to_csv(OUT / "fixes_by_hour.csv", index=False)
    (OUT / "summary.json").write_text(json.dumps(stats, indent=2, default=str))
    print(json.dumps(stats, indent=2, default=str))
    print(by_class.round(1).head(12).to_string())
    spark.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=float, default=0.0, help="fraction of trips to use, e.g. 0.05")
    ap.add_argument("--broadcast", action="store_true", help="broadcast the road cells instead of a shuffle join")
    ap.add_argument("--input", default=str(RAW), help="trips CSV in the Porto format")
    ap.add_argument("--out", default=str(OUT), help="output folder")
    ap.add_argument("--save-matches", action="store_true", help="also write the per-fix road matches (for validation)")
    a = ap.parse_args()
    main(a.sample, a.broadcast, a.input, a.out, a.save_matches)
