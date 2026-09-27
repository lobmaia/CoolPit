"""
Record real Gemini decisions for the demo, so the demo can replay them without API quota.

It plays the race from --start-lap in steps of --step, calls Gemini once per lap, and ACCEPTS every
recommended change (one fixed demo path). The results go to demo/saved_responses.json, which
    python demo/demo_server.py --replay
then serves instead of calling Gemini.

Run (needs a GEMINI_API_KEY with quota left):
    python demo/record_responses.py --start-lap 10 --step 5

If Gemini fails partway (quota, overload), the laps recorded so far are kept. Run the same command
again later and it continues from where it stopped.
"""
import argparse
import copy
import json
import os
import sys
import time
from datetime import datetime

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(DEMO_DIR))
sys.path.insert(0, DEMO_DIR)

import cooling_vest_agent as agent  # noqa: E402
from demo_server import DEFAULT_REPLAY, advance_vest, replay_key  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=os.path.join(os.path.dirname(DEMO_DIR), "sample_input.json"))
    parser.add_argument("--start-lap", type=int, default=10)
    parser.add_argument("--step", type=int, default=5)
    parser.add_argument("--out", default=DEFAULT_REPLAY)
    parser.add_argument("--delay", type=float, default=3.0, help="seconds between Gemini calls (free-tier rate limits)")
    parser.add_argument("--model", help=f"Gemini model to use (default {agent.MODEL}); free-tier quota is per model")
    args = parser.parse_args()
    if args.model:
        agent.MODEL = args.model

    with open(args.input, encoding="utf-8") as f:
        raw = json.load(f)
    raw["current_lap"] = args.start_lap
    _, total_laps = agent._race_data(raw["year"], raw["event"], raw["driver"])

    # Resume: keep responses already recorded for the same path
    saved = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            old = json.load(f)
        if old["meta"]["start_lap"] == args.start_lap and old["meta"]["step"] == args.step:
            saved = {replay_key(r["lap"], r["vest_state"]["setting"]): r for r in old["responses"]}
            print(f"↩️ Found {len(saved)} responses already recorded for this path; reusing them.")

    responses, complete = [], True
    print(f"🎬 Recording {raw['year']} {raw['event']} · {raw['driver']} · laps {args.start_lap}–{total_laps}, "
          f"step {args.step}, every change accepted")
    while raw["current_lap"] <= total_laps:
        lap, vest = raw["current_lap"], raw["vest_state"]
        key = replay_key(lap, vest["setting"])
        if key in saved:
            entry = saved[key]
            print(f"   lap {lap:>2}: reused    {vest['setting']:>6} -> {entry['output']['decision']['new_setting']}")
        else:
            out = agent.decide_vest(raw)
            if not out["source"].startswith("gemini"):
                print(f"❌ Gemini unavailable at lap {lap} (source: {out['source']}). Stopping; "
                      f"run again later to continue.")
                complete = False
                break
            entry = {"lap": lap, "vest_state": copy.deepcopy(vest), "output": out}
            d = out["decision"]
            print(f"   lap {lap:>2}: recorded  {vest['setting']:>6} -> {d['new_setting']:<6} ({out['source']}) "
                  f"{d['reason']}")
            time.sleep(args.delay)
        responses.append(entry)
        raw["vest_state"] = advance_vest(vest, entry["output"]["decision"], True, args.step)
        raw["current_lap"] = lap + args.step

    data = {
        "meta": {
            "race": f"{raw['year']} {raw['event']}", "driver": raw["driver"],
            "start_lap": args.start_lap, "step": args.step, "path": "every recommended change accepted",
            "model": agent.MODEL, "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "complete": complete,
        },
        "responses": responses,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=float)
    status = "complete" if complete else "PARTIAL"
    print(f"💾 Saved {len(responses)} responses ({status}) to {args.out}")


if __name__ == "__main__":
    main()
