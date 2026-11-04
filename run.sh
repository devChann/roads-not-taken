#!/usr/bin/env bash
# Rebuild everything: data download (once), road index (once), the Spark pipeline, the map.
# usage: ./run.sh [--sample 0.05] [--broadcast]
set -euo pipefail
cd "$(dirname "$0")"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home}"
PY=.venv/bin/python

if [ ! -f data/raw/train.csv ]; then
  mkdir -p data/raw
  curl -L -o data/raw/porto.zip \
    "https://archive.ics.uci.edu/static/public/339/taxi+service+trajectory+prediction+challenge+ecml+pkdd+2015.zip"
  (cd data/raw && unzip -o porto.zip && { [ -f train.csv ] || unzip -o train.csv.zip; })
fi
[ -f data/roads/road_cells.parquet ] || $PY prepare_roads.py
$PY pipeline.py "$@"
$PY make_map.py
