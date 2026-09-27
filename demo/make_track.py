"""
One-time helper: saves the circuit outline for the demo's track map (demo/track.json).

Downloads the race's position data with FastF1 (tens of MB, cached in f1_cache/), takes the driver's
fastest lap, and stores ~300 X/Y points plus the official map rotation.

Run:
    python demo/make_track.py                       # uses sample_input.json (2023 Qatar GP, VER)
"""
import json
import math
import os
import sys

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(DEMO_DIR)
sys.path.insert(0, ROOT_DIR)

import fastf1  # noqa: E402
import cooling_vest_agent as agent  # noqa: E402

N_POINTS = 300


def main():
    with open(os.path.join(ROOT_DIR, "sample_input.json"), encoding="utf-8") as f:
        raw = json.load(f)
    fastf1.Cache.enable_cache(agent.CACHE_DIR)
    fastf1.set_log_level("WARNING")
    session = fastf1.get_session(raw["year"], raw["event"], "R")
    session.load(laps=True, telemetry=True, weather=False, messages=False)

    lap = session.laps.pick_drivers(raw["driver"]).pick_fastest()
    pos = lap.get_pos_data()[["X", "Y"]].dropna()
    rotation = float(session.get_circuit_info().rotation)

    # rotate like the official track map
    a = math.radians(rotation)
    pts = [(x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)) for x, y in zip(pos.X, pos.Y)]
    step = max(1, len(pts) // N_POINTS)
    pts = pts[::step]

    # normalise to a 0..1000 box, y flipped for SVG
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    min_x, min_y = min(xs), min(ys)
    scale = 1000 / max(max(xs) - min_x, max(ys) - min_y)
    points = [[round((x - min_x) * scale, 1), round((max(ys) - y) * scale, 1)] for x, y in pts]

    out = {"race": f"{raw['year']} {raw['event']}", "rotation": rotation, "points": points}
    path = os.path.join(DEMO_DIR, "track.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    w = round(max(p[0] for p in points)); h = round(max(p[1] for p in points))
    print(f"✅ Saved {len(points)} points ({w}x{h}) to {path}")


if __name__ == "__main__":
    main()
