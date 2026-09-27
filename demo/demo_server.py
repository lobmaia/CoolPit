"""
CoolPit demo server (Flask)

Connects the pit-wall pop-up (demo/popup.html) to the cooling vest agent (cooling_vest_agent.py)
and replays a race lap by lap.

Run:
    python demo/demo_server.py                  # http://127.0.0.1:5000
    python demo/demo_server.py --host 0.0.0.0   # let teammates on the same Wi-Fi open http://<your-ip>:5000
    python demo/demo_server.py --start-lap 10 --step 5
    python demo/demo_server.py --offline        # skip Gemini, use the rule-based fallback right away
    python demo/demo_server.py --replay         # answer from demo/saved_responses.json (no Gemini quota needed)

API:
    GET  /                → the pop-up page
    GET  /api/state       → current lap, vest state, last decision (no Gemini call)
    POST /api/decide      → ask Gemini for the current lap; returns {decision, ui, source, lap, ...}
    POST /api/respond     → body {"accepted": true|false}; applies the engineer's choice and moves on
                            by --step laps (when no change was needed, "accepted" is ignored).
                            Also returns "simulation": per-lap data for the laps driven with that setting
    GET  /api/track       → circuit outline for the simulation's track map (demo/track.json)
    GET  /vest3d          → teammate's "F1 Cooling Vest 3D.html", with Three.js served from demo/vendor
    POST /api/reset       → back to the start lap and starting vest state

The vest's cooling capacity is simulated here (CAPACITY_USE_PER_LAP), since there is no real vest.
"""
import argparse
import copy
import json
import os
import sys
import threading

from flask import Flask, jsonify, request, send_from_directory

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(DEMO_DIR)
sys.path.insert(0, ROOT_DIR)

import cooling_vest_agent as agent  # noqa: E402

DEFAULT_REPLAY = os.path.join(DEMO_DIR, "saved_responses.json")

# Simulated capacity drain per lap for each setting, in % (placeholder values)
CAPACITY_USE_PER_LAP = {"OFF": 0.0, "LOW": 0.5, "MEDIUM": 1.0, "HIGH": 2.0}

app = Flask(__name__)
lock = threading.Lock()  # one Gemini call at a time
state = {}               # filled by reset_state()
config = {}              # filled in main()


def advance_vest(vest_state, decision, accepted, step):
    """Vest state after the engineer's choice and `step` laps of driving (shared with record_responses.py)."""
    old_laps = vest_state.get("laps_since_change", 0)
    vest = agent.next_vest_state(vest_state, decision, accepted)
    # next_vest_state counts one lap; the demo jumps `step` laps
    changed = vest["setting"] != vest_state["setting"]
    vest["laps_since_change"] = step if changed else old_laps + step
    # simulated capacity drain for the laps driven on the (new) setting
    used = CAPACITY_USE_PER_LAP[vest["setting"]] * step
    vest["capacity_remaining_pct"] = round(max(0.0, vest["capacity_remaining_pct"] - used), 1)
    return vest


def build_simulation(raw, decision, accepted, vest_after, step, total_laps):
    """Per-lap data for the laps driven after the decision, for the page's simulation view."""
    laps, _ = agent._race_data(raw["year"], raw["event"], raw["driver"])
    before = raw["vest_state"]
    use = CAPACITY_USE_PER_LAP[vest_after["setting"]]
    capacity = before["capacity_remaining_pct"]
    rows = []
    for n in range(raw["current_lap"] + 1, min(raw["current_lap"] + step, total_laps) + 1):
        if n not in laps.index:
            continue
        r = laps.loc[n]
        capacity = round(max(0.0, capacity - use), 1)
        rows.append({
            "lap": int(n),
            "heat_index_c": round(float(r["HeatIndex"]), 1),
            "air_temp_c": round(float(r["AirTemp"]), 1),
            "track_temp_c": round(float(r["TrackTemp"]), 1),
            "humidity_pct": round(float(r["Humidity"]), 1),
            "track_status": agent.track_status_label(r["TrackStatus"]),
            "lap_time_s": round(float(r["LapTimeS"]), 3) if r["LapTimeS"] == r["LapTimeS"] else None,
            "capacity_pct": capacity,
        })
    return {
        "decision_lap": raw["current_lap"],
        "recommended": decision["new_setting"],
        "change_required": decision["change_required"],
        "accepted": accepted if decision["change_required"] else None,
        "setting_before": before["setting"],
        "setting_after": vest_after["setting"],
        "capacity_before": before["capacity_remaining_pct"],
        "capacity_use_per_lap": use,
        "laps": rows,
    }


def replay_key(lap, setting):
    return f"{lap}:{setting}"


def load_replay(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["meta"], {replay_key(r["lap"], r["vest_state"]["setting"]): r["output"] for r in data["responses"]}


def reset_state():
    with open(config["input"], encoding="utf-8") as f:
        raw = json.load(f)
    raw["current_lap"] = config["start_lap"] or raw["current_lap"]
    _, total_laps = agent._race_data(raw["year"], raw["event"], raw["driver"])
    state.clear()
    state.update({
        "raw": raw,
        "start_raw": copy.deepcopy(raw),
        "total_laps": total_laps,
        "last": None,        # last decide_vest() output for the current lap
        "history": [],       # what happened at each decided lap
        "finished": False,
    })


def public_state():
    raw = state["raw"]
    return {
        "driver": raw["driver"],
        "race": f"{raw['year']} {raw['event']}",
        "lap": raw["current_lap"],
        "total_laps": state["total_laps"],
        "step": config["step"],
        "vest_state": raw["vest_state"],
        "finished": state["finished"],
        "last": state["last"],
        "history": state["history"],
    }


@app.get("/")
def index():
    return send_from_directory(DEMO_DIR, "popup.html")


VEST3D_FILE = os.path.join(ROOT_DIR, "F1 Cooling Vest 3D.html")   # teammate's 3D page (not modified)
VENDOR_DIR = os.path.join(DEMO_DIR, "vendor")
CDN_TO_LOCAL = {   # served from demo/vendor so the 3D page also works without internet
    "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js": "/vendor/three.min.js",
    "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js": "/vendor/OrbitControls.js",
    "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/environments/RoomEnvironment.js": "/vendor/RoomEnvironment.js",
    "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/utils/BufferGeometryUtils.js": "/vendor/BufferGeometryUtils.js",
}


@app.get("/vest3d")
def vest3d():
    """The 3D cooling vest page, with CDN script URLs swapped for local copies when they exist."""
    if not os.path.exists(VEST3D_FILE):
        return "F1 Cooling Vest 3D.html not found in the project folder", 404
    with open(VEST3D_FILE, encoding="utf-8") as f:
        page = f.read()
    for cdn, local in CDN_TO_LOCAL.items():
        if os.path.exists(os.path.join(VENDOR_DIR, os.path.basename(local))):
            page = page.replace(cdn, local)
    return page, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.get("/vendor/<path:name>")
def vendor(name):
    return send_from_directory(VENDOR_DIR, name)


@app.get("/api/track")
def get_track():
    path = os.path.join(DEMO_DIR, "track.json")
    if not os.path.exists(path):
        return jsonify({"error": "No track.json. Run python demo/make_track.py once."}), 404
    return send_from_directory(DEMO_DIR, "track.json")


@app.get("/api/state")
def get_state():
    return jsonify(public_state())


@app.post("/api/decide")
def decide():
    if state["finished"]:
        return jsonify({"error": "Race finished. POST /api/reset to start again."}), 409
    with lock:
        raw = state["raw"]
        replay = config.get("replay", {})
        saved = replay.get(replay_key(raw["current_lap"], raw["vest_state"]["setting"]))
        if saved:
            out = {**copy.deepcopy(saved), "replayed": True}
        else:
            if replay:
                print(f"ℹ️ No saved response for lap {raw['current_lap']} with vest "
                      f"{raw['vest_state']['setting']} (not recorded, or off the recorded path) → live call")
            out = {**agent.decide_vest(raw), "replayed": False}
        state["last"] = {**out, "lap": raw["current_lap"], "total_laps": state["total_laps"],
                         "driver": raw["driver"], "vest_state": raw["vest_state"]}
    return jsonify(state["last"])


@app.post("/api/respond")
def respond():
    last = state["last"]
    if last is None or last["lap"] != state["raw"]["current_lap"]:
        return jsonify({"error": "No decision for this lap yet. POST /api/decide first."}), 409
    accepted = bool((request.get_json(silent=True) or {}).get("accepted", False))

    with lock:
        raw, decision, step = state["raw"], last["decision"], config["step"]
        vest = advance_vest(raw["vest_state"], decision, accepted, step)
        simulation = build_simulation(raw, decision, accepted, vest, step, state["total_laps"])
        # keep the capacity shown at the end of the simulation consistent with the vest state
        if simulation["laps"]:
            vest["capacity_remaining_pct"] = simulation["laps"][-1]["capacity_pct"]

        state["history"].append({
            "lap": raw["current_lap"],
            "recommended": decision["new_setting"],
            "previous": decision["previous_setting"],
            "change_required": decision["change_required"],
            "accepted": accepted if decision["change_required"] else None,
            "applied_setting": vest["setting"],
            "source": last["source"],
        })
        raw["vest_state"] = vest
        next_lap = raw["current_lap"] + step
        if next_lap > state["total_laps"]:
            state["finished"] = True
        else:
            raw["current_lap"] = next_lap
        state["last"] = None
    return jsonify({**public_state(), "simulation": simulation})


@app.post("/api/reset")
def reset():
    with lock:
        reset_state()
    return jsonify(public_state())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=os.path.join(ROOT_DIR, "sample_input.json"))
    parser.add_argument("--start-lap", type=int, help="first lap to decide (default: current_lap in the input)")
    parser.add_argument("--step", type=int, help="laps to advance after each decision (default 5)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--offline", action="store_true", help="skip Gemini and use the rule-based fallback")
    parser.add_argument("--replay", nargs="?", const=DEFAULT_REPLAY, metavar="FILE",
                        help="answer from saved Gemini responses (default file: demo/saved_responses.json)")
    args = parser.parse_args()

    replay = {}
    if args.replay:
        meta, replay = load_replay(args.replay)
        # use the recorded start lap and step unless given explicitly
        args.start_lap = args.start_lap or meta["start_lap"]
        args.step = args.step or meta["step"]
        print(f"🎞️ Replay mode: {len(replay)} saved responses from {args.replay} "
              f"(recorded path: start lap {meta['start_lap']}, step {meta['step']}, every change accepted)")
    config.update({"input": args.input, "start_lap": args.start_lap, "step": args.step or 5, "replay": replay})

    if args.offline:
        def no_gemini(_prompt):
            raise RuntimeError("offline mode")
        agent.call_gemini = no_gemini
        print("📴 Offline mode: Gemini is skipped, decisions use the rule-based fallback")

    print("⏳ Loading race data...")
    reset_state()
    print(f"✅ {state['raw']['year']} {state['raw']['event']} · {state['raw']['driver']} · "
          f"starting at lap {state['raw']['current_lap']}/{state['total_laps']}, step {config['step']}")
    print(f"🌐 Open http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
