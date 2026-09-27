"""
Cooling Vest Agent (prototype)

Live race data (FastF1 stands in for the live feed) + vest state  →  prompt  →  Gemini  →  vest setting
The model does NOT use biometric data.

Flow (approval-based):
    1. decide_vest(input) → recommendation + UI payload for the pit-wall pop-up
    2. The race engineer clicks Accept or Reject in the pop-up
    3. next_vest_state(vest_state, decision, accepted) → vest state to send with the next lap's input

Usage from Python:
    from cooling_vest_agent import decide_vest, next_vest_state
    out = decide_vest(input_dict)           # {"decision": {...}, "ui": {...}, "source": "..."}
    vest_state = next_vest_state(input_dict["vest_state"], out["decision"], accepted=True)

Usage from the command line:
    python cooling_vest_agent.py --make-sample                    # create sample_input.json
    python cooling_vest_agent.py --dry-run                        # print the prompt only (no API call)
    python cooling_vest_agent.py                                  # call Gemini for current_lap in the JSON
    python cooling_vest_agent.py --laps 10,30,50                  # decide at several laps
    python cooling_vest_agent.py --json                           # print the full output JSON (decision + ui)

Input JSON format (race data for the given lap is looked up from FastF1 in this prototype):
{
  "driver": "VER", "year": 2023, "event": "Qatar Grand Prix", "current_lap": 45,
  "vest_state": {"setting": "LOW", "capacity_remaining_pct": 70, "laps_since_change": 6}
}
vest_state.setting is one of OFF, LOW, MEDIUM, HIGH.
NOTE: zone and fallback thresholds below are placeholders for the prototype.
"""
import argparse
import json
import os
from datetime import datetime
from functools import lru_cache
from typing import Literal

import fastf1
import pandas as pd
from pydantic import BaseModel, Field

MODEL = "gemini-3.8-flash"
# Tried when the main model is overloaded or out of quota. Must be a different model:
# free-tier quota is per model, and "gemini-flash-latest" is just an alias of gemini-3.8-flash.
BACKUP_MODEL = "gemini-3.7-flash"
RETRY_WAITS_SEC = [2, 5]             # waits before retrying on 503/429
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # paths work no matter which folder you run from
CACHE_DIR = os.path.join(BASE_DIR, "f1_cache")
LOG_FILE = os.path.join(BASE_DIR, "vest_decisions.jsonl")

SETTINGS = ["OFF", "LOW", "MEDIUM", "HIGH"]
TRACK_STATUS = {"1": "GREEN", "2": "YELLOW", "4": "SAFETY_CAR", "5": "RED_FLAG", "6": "VSC", "7": "VSC_ENDING"}

# Pop-up theme (zone) from the heat index - placeholder thresholds, computed by code (not by Gemini)
ZONE_CRITICAL_HEAT_C = 35.0   # heat index at or above → CRITICAL_HEAT
ZONE_COLD_SNAP_C = 20.0       # heat index below → COLD_SNAP (in between → GREEN_ZONE)
ZONES = {
    "CRITICAL_HEAT": {"title": "CRITICAL HEAT", "status_text": "Above threshold"},
    "GREEN_ZONE": {"title": "GREEN ZONE", "status_text": "Optimal range"},
    "COLD_SNAP": {"title": "COLD SNAP", "status_text": "Well below threshold"},
}


# =============================================================================
# 1. Prompt templates (English)
# =============================================================================
SYSTEM_PROMPT = """\
# ROLE
You are "CoolPit", an AI cooling-strategy engineer on a Formula 1 pit wall.
The driver wears a cooling vest with four settings: OFF, LOW, MEDIUM and HIGH. Higher settings cool more
but use the vest's limited cooling capacity faster.
You are called once per lap with live race data. Each time, you recommend the vest setting for the next lap:
keep the current setting or change it (for example LOW -> MEDIUM, HIGH -> LOW, LOW -> OFF).
Your recommendation appears as a pop-up on the pit wall. The race engineer accepts or rejects it; only an
accepted change is applied to the vest. If a change is rejected, the current setting stays and you will see
it again in the next lap's vest_state.
You have no biometric data. Do not make claims about the driver's health or physical condition.

# INPUT
You receive one JSON object with:
- "race_context": live race data for the current lap: lap and laps remaining, track status, pit lap flag,
  weather (air and track temperature, humidity, heat index and its trend over recent laps), tyre and pace.
- "vest_state": the current vest setting, remaining cooling capacity (%), and laps since the setting last changed.

# DECISION RULES
1. Use ONLY the numbers provided. Do not invent or recalculate values.
2. Heat load drives the setting: a higher heat index, especially with high humidity, calls for a higher
   setting. A rising heat index trend is a reason to step up; a falling trend is a reason to step down.
3. Driver workload matters: under a safety car, VSC or red flag the driver works less and generates less
   heat, so a lower setting is usually enough. Green-flag racing needs more cooling than neutralised laps.
4. Manage capacity over the whole race: compare remaining capacity with laps remaining. If capacity is low
   for the laps left, prefer lower settings. If few laps remain and capacity is ample, a higher setting is fine.
5. Change gradually: prefer moving one step at a time (e.g. LOW -> MEDIUM). Jump two or more steps only for a
   clear reason, such as a safety car starting or ending.
6. Avoid frequent switching: if the setting changed only recently (vest_state.laps_since_change is small),
   keep it unless conditions have clearly changed.
7. On a pit lap (race_context.pit_lap is true) lap times are not representative; ignore pace on that lap.
8. When uncertain, keep the current setting (vest_state.setting).

# OUTPUT
Return ONLY a JSON object that matches the given schema:
- "new_setting": "OFF", "LOW", "MEDIUM" or "HIGH" (the recommended setting for the next lap; may equal
  the current one)
- "confidence": number between 0 and 1
- "reason": one short sentence (max 15 words) shown in the pop-up under the recommendation
- "monitor": exactly 3 short items (max 6 words each) the engineer should keep an eye on over the next laps
- "key_factors": 2-4 short bullet strings citing the specific numbers you used
"""

USER_PROMPT_TEMPLATE = """\
Choose the cooling vest setting for {driver} for the lap after lap {lap} of the {race}.

DATA:
```json
{payload_json}
```
"""


class VestDecision(BaseModel):
    new_setting: Literal["OFF", "LOW", "MEDIUM", "HIGH"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    monitor: list[str]
    key_factors: list[str]


# =============================================================================
# 2. Race data (FastF1 stands in for the live race feed)
# =============================================================================
def load_race(year, event):
    os.makedirs(CACHE_DIR, exist_ok=True)   # f1_cache/ is git-ignored, so a fresh clone does not have it
    fastf1.Cache.enable_cache(CACHE_DIR)
    fastf1.set_log_level("CRITICAL")
    session = fastf1.get_session(year, event, "R")
    session.load(laps=True, telemetry=False, weather=True, messages=False)
    return session


def lap_table(session, driver):
    """One row per lap for this driver, with the weather at the start of that lap."""
    laps = session.laps.pick_drivers(driver)[
        ["LapNumber", "LapStartTime", "LapTime", "Compound", "TyreLife", "Position", "TrackStatus", "Stint",
         "PitInTime", "PitOutTime"]
    ].dropna(subset=["LapStartTime"]).sort_values("LapStartTime")
    weather = session.weather_data[["Time", "AirTemp", "TrackTemp", "Humidity", "Rainfall"]].sort_values("Time")
    merged = pd.merge_asof(laps, weather, left_on="LapStartTime", right_on="Time", direction="backward")
    merged["LapTimeS"] = merged["LapTime"].dt.total_seconds()
    merged["LapNumber"] = merged["LapNumber"].astype(int)
    merged["PitLap"] = merged["PitInTime"].notna() | merged["PitOutTime"].notna()
    merged["HeatIndex"] = [heat_index_c(t, h) for t, h in zip(merged["AirTemp"], merged["Humidity"])]
    return merged.set_index("LapNumber")


def heat_index_c(temp_c, rh):
    """NOAA heat index (Rothfusz regression), returned in Celsius."""
    t = temp_c * 9 / 5 + 32
    hi = 0.5 * (t + 61 + (t - 68) * 1.2 + rh * 0.094)
    if (hi + t) / 2 >= 80:
        hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh
              - 0.00683783 * t * t - 0.05481717 * rh * rh + 0.00122874 * t * t * rh
              + 0.00085282 * t * rh * rh - 0.00000199 * t * t * rh * rh)
    return (hi - 32) * 5 / 9


def track_status_label(code):
    code = str(code) if pd.notna(code) else "1"
    labels = [TRACK_STATUS.get(c, c) for c in code]
    for priority in ["RED_FLAG", "SAFETY_CAR", "VSC", "VSC_ENDING", "YELLOW"]:
        if priority in labels:
            return priority
    return "GREEN"


def slope_per_lap(values):
    n = len(values)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2, sum(values) / n
    num = sum((x - mx) * (y - my) for x, y in enumerate(values))
    den = sum((x - mx) ** 2 for x in range(n))
    return num / den


def build_race_context(laps, lap, total_laps):
    row = laps.loc[lap]
    # pace only from clean laps of the current stint (no pit in/out laps)
    stint = laps[(laps["Stint"] == row["Stint"]) & ~laps["PitLap"]].loc[:lap, "LapTimeS"].dropna()
    recent = stint.iloc[-5:]
    heat_recent = list(laps.loc[max(1, lap - 4):lap, "HeatIndex"])
    return {
        "lap": lap,
        "total_laps": total_laps,
        "laps_remaining": total_laps - lap,
        "track_status": track_status_label(row["TrackStatus"]),
        "pit_lap": bool(row["PitLap"]),
        "position": int(row["Position"]) if pd.notna(row["Position"]) else None,
        "tyre": {"compound": row["Compound"], "age_laps": int(row["TyreLife"]) if pd.notna(row["TyreLife"]) else None},
        "pace": {
            "last_lap_s": round(row["LapTimeS"], 3) if pd.notna(row["LapTimeS"]) else None,
            "avg_last_5_laps_s": round(recent.mean(), 3) if len(recent) else None,
            "delta_to_stint_best_s": round(recent.mean() - stint.min(), 3) if len(stint) else None,
        },
        "weather": {
            "air_temp_c": round(row["AirTemp"], 1),
            "track_temp_c": round(row["TrackTemp"], 1),
            "humidity_pct": round(row["Humidity"], 1),
            "heat_index_c": round(row["HeatIndex"], 1),
            "heat_index_trend_per_lap_last5": round(slope_per_lap(heat_recent), 3),
            "rain": bool(row["Rainfall"]),
        },
    }


def make_sample(path, driver="VER", year=2023, event="Qatar Grand Prix", current_lap=45):
    sample = {
        "driver": driver, "year": year, "event": event, "current_lap": current_lap,
        "vest_state": {"setting": "LOW", "capacity_remaining_pct": 70, "laps_since_change": 6},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sample, f, indent=2, ensure_ascii=False)
    print(f"✅ {path} created ({year} {event}, {driver}, lap {current_lap})")


# =============================================================================
# 3. Payload and prompt
# =============================================================================
def build_payload(raw, laps, total_laps, lap):
    return {
        "driver": raw["driver"],
        "race_context": build_race_context(laps, lap, total_laps),
        "vest_state": raw["vest_state"],
    }


def build_user_prompt(raw, payload):
    return USER_PROMPT_TEMPLATE.format(
        driver=raw["driver"], lap=payload["race_context"]["lap"],
        race=f"{raw['year']} {raw['event']}",
        payload_json=json.dumps(payload, indent=2, ensure_ascii=False, default=float),
    )


# =============================================================================
# 4. Gemini call + fallback
# =============================================================================
def rule_based_fallback(payload):
    """Simple heat-index rule used only when Gemini is unavailable (placeholder thresholds)."""
    rc, vs = payload["race_context"], payload["vest_state"]
    hi = rc["weather"]["heat_index_c"]
    level = 3 if hi >= 40 else 2 if hi >= 35 else 1 if hi >= 30 else 0
    if rc["track_status"] in ("SAFETY_CAR", "VSC", "RED_FLAG"):
        level = max(0, level - 1)
    if vs.get("capacity_remaining_pct", 100) <= 0:
        level = 0
    return VestDecision(new_setting=SETTINGS[level], confidence=0.5,
                        reason="Rule-based fallback from heat index (Gemini unavailable).",
                        monitor=["Heat index", "Track status", "Remaining vest capacity"],
                        key_factors=[f"heat index {hi}C", f"track status {rc['track_status']}"])


def call_gemini(user_prompt):
    """Returns (decision, model_used). Retries on overload, then tries BACKUP_MODEL; raises if all fail."""
    import time
    from google import genai
    from google.genai import types
    client = genai.Client()  # reads GEMINI_API_KEY
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=VestDecision,
        temperature=0.2,
    )
    last_error = None
    for model in (MODEL, BACKUP_MODEL):
        for wait in [0] + RETRY_WAITS_SEC:
            time.sleep(wait)
            try:
                response = client.models.generate_content(model=model, contents=user_prompt, config=config)
                parsed = response.parsed or VestDecision.model_validate_json(response.text)
                return parsed, model
            except Exception as e:
                last_error = e
                if not any(code in str(e) for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                    raise  # not a temporary error → no point retrying
    raise last_error


# =============================================================================
# 5. Decision, pop-up payload, engineer approval
# =============================================================================
def heat_zone(heat_index):
    if heat_index >= ZONE_CRITICAL_HEAT_C:
        return "CRITICAL_HEAT"
    if heat_index < ZONE_COLD_SNAP_C:
        return "COLD_SNAP"
    return "GREEN_ZONE"


def to_ui_payload(decision, payload):
    """Fields for the pit-wall pop-up (HTML schemas/popup_ui*.html)."""
    w = payload["race_context"]["weather"]
    zone = decision["zone"]
    changing = decision["change_required"]
    return {
        "zone": zone,                                   # CRITICAL_HEAT / GREEN_ZONE / COLD_SNAP → colour theme
        "title": ZONES[zone]["title"],                  # .title
        "status_text": ZONES[zone]["status_text"],      # .status
        "track_temp_c": w["track_temp_c"],              # gauge readout + "Track"
        "air_temp_c": w["air_temp_c"],                  # "Air"
        "humidity_pct": w["humidity_pct"],              # "Humidity"
        "heat_index_c": w["heat_index_c"],
        "switch_to": decision["new_setting"],           # .rec-value
        "from": decision["previous_setting"],           # .rec-current
        "change_required": changing,                    # false → show a "keep current setting" state, no buttons
        "reason": decision["reason"],                   # .rec-reason
        "monitor": decision["monitor"],                 # .risk-item x3
        "accept_label": f"Change to {decision['new_setting']}" if changing else None,
        "reject_label": f"Stay with {decision['previous_setting']}" if changing else None,
    }


def next_vest_state(vest_state, decision, accepted):
    """Vest state for the next lap's input, after the engineer accepts or rejects the pop-up.
    capacity_remaining_pct is left as is: in a real system it comes from the vest hardware."""
    state = dict(vest_state)
    if decision["change_required"] and accepted:
        state["setting"] = decision["new_setting"]
        state["laps_since_change"] = 0
    else:
        state["laps_since_change"] = state.get("laps_since_change", 0) + 1
    return state


def decide(raw, laps, total_laps, lap, dry_run=False):
    """Returns (decision dict, source, payload, user_prompt), or None on dry run."""
    payload = build_payload(raw, laps, total_laps, lap)
    user_prompt = build_user_prompt(raw, payload)

    if dry_run:
        print("=" * 30, "SYSTEM PROMPT", "=" * 30)
        print(SYSTEM_PROMPT)
        print("=" * 30, "USER PROMPT", "=" * 30)
        print(user_prompt)
        return None

    try:
        gemini_decision, model_used = call_gemini(user_prompt)
        source = f"gemini:{model_used}"
    except Exception as e:
        print(f"⚠️ Gemini call failed ({type(e).__name__}: {str(e)[:120]}) → rule-based fallback")
        gemini_decision, source = rule_based_fallback(payload), "fallback"

    current = raw["vest_state"].get("setting", "OFF")
    decision = {
        "lap": lap,
        "previous_setting": current,
        **gemini_decision.model_dump(),
        "change_required": gemini_decision.new_setting != current,
        "zone": heat_zone(payload["race_context"]["weather"]["heat_index_c"]),
    }

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "lap": lap, "source": source,
                            "payload": payload, "decision": decision}, default=float) + "\n")
    return decision, source, payload, user_prompt


@lru_cache(maxsize=8)
def _race_data(year, event, driver):
    """Load a race once and keep it in memory (FastF1 loading takes seconds)."""
    session = load_race(year, event)
    laps = lap_table(session, driver)
    return laps, int(session.total_laps or laps.index.max())


def decide_vest(raw, lap=None):
    """Main entry point for teammates' code. raw = input dict (see module docstring)."""
    laps, total_laps = _race_data(raw["year"], raw["event"], raw["driver"])
    decision, source, payload, _ = decide(raw, laps, total_laps, lap or raw["current_lap"])
    return {"decision": decision, "ui": to_ui_payload(decision, payload), "source": source}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--make-sample", action="store_true", help="create sample_input.json")
    parser.add_argument("--input", default="sample_input.json")
    parser.add_argument("--laps", help="comma-separated laps to evaluate, e.g. 10,30,50 (default: current_lap)")
    parser.add_argument("--dry-run", action="store_true", help="print prompts without calling Gemini")
    parser.add_argument("--json", action="store_true", help="print the full output JSON (decision + ui)")
    args = parser.parse_args()

    if args.make_sample:
        make_sample(args.input)
        return

    with open(args.input, encoding="utf-8") as f:
        raw = json.load(f)
    lap_list = [int(x) for x in args.laps.split(",")] if args.laps else [raw["current_lap"]]

    if args.dry_run:
        laps, total_laps = _race_data(raw["year"], raw["event"], raw["driver"])
        for lap in lap_list:
            decide(raw, laps, total_laps, lap, dry_run=True)
        return

    for lap in lap_list:
        out = decide_vest(raw, lap)
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False, default=float))
            continue
        d, ui = out["decision"], out["ui"]
        change = f"{d['previous_setting']} -> {d['new_setting']}" if d["change_required"] else f"KEEP {d['new_setting']}"
        print(f"\n🏁 Lap {lap} | {ui['title']} | HI {ui['heat_index_c']}C | track {ui['track_temp_c']}C")
        print(f"   🧊 {change}  (conf {d['confidence']:.2f}, source: {out['source']})")
        print(f"   💬 {d['reason']}")
        print(f"   👀 Monitor: {' / '.join(d['monitor'])}")


if __name__ == "__main__":
    main()
