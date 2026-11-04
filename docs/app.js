(() => {
  "use strict";
  const MAP_STYLE = "https://tiles.openfreemap.org/styles/positron";
  const CLASSES = [["m", "Motorway & trunk"], ["p", "Primary"], ["s", "Secondary"], ["t", "Tertiary"], ["r", "Residential"], ["o", "Other"]];
  const CLASS_NAME = Object.fromEntries(CLASSES);
  const MODES = [["all", "🗺️ All roads"], ["gaps", "👻 Never driven"], ["regular", "🔁 Driven 10+ times"]];
  const GAP = "#3b5bff", FAINT = "#d8d2cc";
  const RAMP = ["interpolate", ["linear"], ["ln", ["+", ["get", "t"], 1]],
    0.69, "#ffd1a1", 2.4, "#ffa94d", 4.6, "#ff6b3d", 6.9, "#e8336d", 9.3, "#7a1fa2"];
  const S = { mode: "all", classes: new Set(CLASSES.map(c => c[0])), roads: null, map: null };
  const $ = id => document.getElementById(id);
  const fmt = n => Number(n).toLocaleString("en");
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };

  function stats(s) {
    const r = s.run, v = s.validation;
    const cards = [
      ["blue", "🛰️", `${(r.fixes_total / 1e6).toFixed(1)}M`, `GPS fixes cleaned and matched to OpenStreetMap roads (${r.match_rate_pct}% of clean fixes matched)`],
      ["pink", "🛣️", `${Math.round(r.road_km_driven_pct)}%`, `of Porto's ${fmt(r.road_km)} km of drivable road driven at least once`],
      ["yellow", "🏘️", `${pct(s.classes.find(c => c.group === "r"), "regular")}%`, `of residential streets driven 10+ times, against ${pct(s.classes.find(c => c.group === "m"), "regular")}% of motorways and trunk roads`],
      ["mint", "🎯", `${v.match_correct_street_pct}%`, `of fixes matched to the correct street on synthetic drives with known routes`],
    ];
    $("stats").replaceChildren(...cards.map(([c, i, b, t]) => {
      const d = el("div", `stat ${c}`); d.append(el("div", "top", `<i>${i}</i><b>${b}</b>`), el("span", null, t)); return d;
    }));
  }
  const pct = (c, k) => Math.round(100 * c[k] / c.km);

  function classBars(s) {
    $("classbars").replaceChildren(...s.classes.map(c => {
      const reg = 100 * c.regular / c.km, once = 100 * (c.driven - c.regular) / c.km, never = 100 - reg - once;
      const row = el("div", "barrow");
      row.append(el("span", null, `<b>${CLASS_NAME[c.group]}</b>`));
      const tr = el("div", "track");
      tr.append(Object.assign(el("i"), { style: `width:${reg}%;background:#e8336d`, title: `${reg.toFixed(0)}% driven 10+ times` }),
        Object.assign(el("i"), { style: `width:${once}%;background:#ffb38a`, title: `${once.toFixed(0)}% driven 1–9 times` }),
        Object.assign(el("i"), { style: `width:${never}%;background:var(--gap)`, title: `${never.toFixed(0)}% never driven` }));
      row.append(tr, el("span", "val", `${Math.round(reg + once)}% driven · ${fmt(Math.round(c.km))} km`));
      return row;
    }));
  }

  function race(s) {
    const b = s.join.broadcast.match_s, sh = s.join.shuffle.match_s, max = Math.max(b, sh);
    const lane = (name, sec, cls) => {
      const l = el("div", "lane"); l.append(el("span", null, name));
      l.append(Object.assign(el("div", `bar ${cls}`), { style: `width:${100 * sec / max}%` }), el("span", "s", `${sec.toFixed(0)} s`));
      return l;
    };
    $("race").replaceChildren(lane("Broadcast", b, "fast"), lane("Shuffle", sh, "slow"));
  }

  function validation(s) {
    const v = s.validation;
    const rows = [["Synthetic drives / GPS fixes", `${fmt(v.synthetic_trips)} / ${fmt(v.fixes)}`],
      ["Injected jumps removed", `${v.jumps_removed_pct}%`], ["Good fixes wrongly removed", `${v.good_fixes_wrongly_dropped_pct}%`],
      ["Matched to the exact segment", `${v.match_exact_pct}%`], ["Exact or adjacent segment", `${v.match_exact_or_adjacent_pct}%`],
      ["<b>On the correct street</b>", `<b>${v.match_correct_street_pct}%</b>`]];
    $("valtable").innerHTML = rows.map(([k, x]) => `<tr><td>${k}</td><td class="num">${x}</td></tr>`).join("");
  }

  // ---------- controls ----------
  function controls() {
    $("modes").replaceChildren(...MODES.map(([k, label]) => {
      const b = el("button", "chip", label); b.type = "button"; b.setAttribute("aria-pressed", k === S.mode);
      b.onclick = () => { S.mode = k; [...$("modes").children].forEach(c => c.setAttribute("aria-pressed", c === b)); apply(); };
      return b;
    }));
    $("classes").replaceChildren(...CLASSES.map(([k, label]) => {
      const b = el("button", "chip", label); b.type = "button"; b.setAttribute("aria-pressed", "true");
      b.onclick = () => {
        if (S.classes.has(k) && S.classes.size > 1) S.classes.delete(k); else S.classes.add(k);
        b.setAttribute("aria-pressed", S.classes.has(k)); apply();
      };
      return b;
    }));
  }

  function shown() {
    let km = 0, gaps = 0, n = 0;
    for (const f of S.roads.features) {
      const p = f.properties;
      if (!S.classes.has(p.c)) continue;
      km += p.l; n++; if (p.t === 0) gaps += p.l;
    }
    const pctGap = km ? Math.round(100 * gaps / km) : 0;
    $("shown").innerHTML = `<span>Selected classes</span><b>${fmt(Math.round(km / 1000))} km · ${fmt(n)} segments</b>
      <span><b style="font-size:15px;color:var(--gap)">${fmt(Math.round(gaps / 1000))} km never driven</b> (${pctGap}%)</span>`;
  }

  function longest() {
    const by = new Map();
    for (const f of S.roads.features) {
      const p = f.properties;
      if (p.t !== 0 || !p.n || !S.classes.has(p.c)) continue;
      const g = by.get(p.n) || { name: p.n, m: 0, box: [180, 90, -180, -90], c: p.c };
      g.m += p.l;
      for (const [x, y] of f.geometry.coordinates) {
        g.box[0] = Math.min(g.box[0], x); g.box[1] = Math.min(g.box[1], y); g.box[2] = Math.max(g.box[2], x); g.box[3] = Math.max(g.box[3], y);
      }
      by.set(p.n, g);
    }
    const top = [...by.values()].sort((a, b) => b.m - a.m).slice(0, 8);
    $("longest").replaceChildren(...top.map(g => {
      const li = el("li"), b = el("button", null, `<span>${g.name}</span><span class="km">${(g.m / 1000).toFixed(1)} km</span>`);
      b.type = "button";
      b.onclick = () => S.map.fitBounds([[g.box[0], g.box[1]], [g.box[2], g.box[3]]], { padding: 80, maxZoom: 16, duration: 900 });
      li.append(b); return li;
    }));
  }

  function apply() {
    const m = S.map;
    if (!m || !m.getLayer("driven")) return;
    const cls = ["in", ["get", "c"], ["literal", [...S.classes]]];
    m.setFilter("driven", ["all", cls, [">", ["get", "t"], 0]]);
    m.setFilter("gaps", ["all", cls, ["==", ["get", "t"], 0]]);
    const color = S.mode === "gaps" ? FAINT : S.mode === "regular" ? ["case", [">=", ["get", "t"], 10], RAMP, FAINT] : RAMP;
    m.setPaintProperty("driven", "line-color", color);
    m.setPaintProperty("driven", "line-opacity", S.mode === "gaps" ? 0.55 : 0.92);
    m.setLayoutProperty("gaps", "visibility", S.mode === "regular" ? "none" : "visible");
    shown(); longest();
  }

  // ---------- map ----------
  function initMap() {
    const W = ["match", ["get", "c"], "m", 3.2, "p", 2.6, "s", 2.2, "t", 1.8, "r", 1.15, 1.0];
    const width = ["interpolate", ["linear"], ["zoom"], 10, ["*", W, 0.5], 13, W, 16, ["*", W, 2.6]];
    const gapWidth = ["interpolate", ["linear"], ["zoom"], 10, ["*", W, 0.65], 13, ["*", W, 1.3], 16, ["*", W, 3.2]];
    const map = S.map = new maplibregl.Map({ container: "map", style: MAP_STYLE, center: [-8.615, 41.16], zoom: 12.2,
      attributionControl: { compact: true }, cooperativeGestures: false });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.on("load", () => {
      map.addSource("roads", { type: "geojson", data: S.roads });
      map.addLayer({ id: "driven", type: "line", source: "roads", layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": RAMP, "line-width": width, "line-opacity": 0.92 } });
      map.addLayer({ id: "gaps", type: "line", source: "roads", layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": GAP, "line-width": gapWidth, "line-opacity": 0.95 } });
      apply();
      $("loading").style.opacity = 0; setTimeout(() => $("loading").remove(), 350);
      const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
      const label = p => p.n || `Unnamed ${p.h.replace("_", " ")}`;
      for (const id of ["driven", "gaps"]) {
        map.on("mousemove", id, e => {
          map.getCanvas().style.cursor = "pointer";
          const p = e.features[0].properties;
          pop.setLngLat(e.lngLat).setHTML(p.t ? `<b>${label(p)}</b><br>${fmt(p.t)} trips · ${fmt(p.x)} taxis · ${fmt(p.d)} days`
            : `<b>${label(p)}</b><br><span style="color:${GAP};font-weight:700">never driven</span>`).addTo(map);
        });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; pop.remove(); });
        map.on("click", id, e => info(e.features[0].properties));
      }
    });
  }

  function info(p) {
    const box = $("info");
    box.hidden = false;
    box.innerHTML = `<span class="name">${p.n || "Unnamed road"}</span>
      <dl><dt>Class</dt><dd>${p.h.replace("_", " ")}</dd><dt>Length</dt><dd>${fmt(p.l)} m</dd>
      <dt>Trips</dt><dd>${fmt(p.t)}</dd><dt>Taxis</dt><dd>${fmt(p.x)}</dd><dt>Days</dt><dd>${fmt(p.d)}</dd></dl>
      ${p.t ? "" : `<span style="color:${GAP};font-weight:700">👻 No taxi drove this segment in six months</span>`}`;
  }

  async function main() {
    const [summary, roads] = await Promise.all([fetch("data/summary.json").then(r => r.json()), fetch("data/roads.geojson").then(r => r.json())]);
    S.roads = roads;
    stats(summary); classBars(summary); race(summary); validation(summary); controls(); shown(); longest(); initMap();
  }
  main().catch(err => { $("loading").textContent = "Could not load the data: " + err.message; });
})();
