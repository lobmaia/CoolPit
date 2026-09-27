# CoolPit — Cooling Vest Agent (prototype)

CoolPit recommends a cooling vest setting (**OFF / LOW / MEDIUM / HIGH**) once per lap, based on live race data
and the current vest state. The recommendation appears as a pop-up on the pit wall; the race engineer
**accepts or rejects** it, and only an accepted change is applied.

- The model uses **no biometric data**.
- In this prototype, historical **FastF1** data (2023 Qatar GP) stands in for the live race feed.
- The decision is made by **Gemini**, using the prompt in `cooling_vest_agent.py`.

```
input JSON ──▶ race context for the lap (weather, heat index + trend, track status, tyre, pace)
           ──▶ prompt ──▶ Gemini ──▶ decision + pop-up payload
           ──▶ engineer clicks Accept / Reject ──▶ next_vest_state() ──▶ next lap's input
```

## Files

| File | What it is |
|---|---|
| `cooling_vest_agent.py` | Core: prompt, race data, Gemini call, fallback, pop-up payload, approval flow |
| `vest_viewer.py` | Runs one call and writes an HTML report (input, prompt, output, pop-up payload) |
| `sample_input.json` | Example input |
| `sample_output.json` | Example output from a real Gemini call (lap 45) |
| `test_gemini.py` | Checks that your Gemini API key works |
| `HTML schemas/popup_ui*.html` | Pit-wall pop-up mockups (critical heat / green zone / cold snap) |

## Setup

```powershell
pip install -r requirements.txt
setx GEMINI_API_KEY "your_key"     # get a free key at https://aistudio.google.com, then restart VS Code
python test_gemini.py              # should print "✅ Success!" with an answer
```

The first run downloads the 2023 Qatar GP data into `f1_cache/` (about 30 seconds). Later runs read the cache.

## Run

```powershell
python cooling_vest_agent.py                 # one decision for current_lap in sample_input.json
python cooling_vest_agent.py --json          # full output JSON (decision + ui)
python cooling_vest_agent.py --laps 10,30,50 # several laps
python cooling_vest_agent.py --dry-run       # print the prompt only, no API call
python vest_viewer.py --lap 45               # one call + HTML report in your browser
```

Each call uses one Gemini request. The free tier has rate limits, so avoid calling every second.

## Use it from your code (Python)

```python
from cooling_vest_agent import decide_vest, next_vest_state

out = decide_vest(input_dict)   # {"decision": {...}, "ui": {...}, "source": "gemini:..."}

# show out["ui"] in the pop-up, then when the engineer clicks:
input_dict["vest_state"] = next_vest_state(input_dict["vest_state"], out["decision"], accepted=True)
input_dict["current_lap"] += 1  # next lap
```

`decide_vest` loads the race once and keeps it in memory, so later calls are fast (Gemini takes ~5–15 s).
If Gemini fails (overload, rate limit), it retries, tries a backup model, then falls back to a simple
heat-index rule. `source` tells you which one produced the answer.

## Input

```json
{
  "driver": "VER",
  "year": 2023,
  "event": "Qatar Grand Prix",
  "current_lap": 45,
  "vest_state": {"setting": "LOW", "capacity_remaining_pct": 70, "laps_since_change": 6}
}
```

| Field | Meaning |
|---|---|
| `driver`, `year`, `event` | Which race and driver (used to look up FastF1 data in this prototype) |
| `current_lap` | Lap the decision is made at |
| `vest_state.setting` | Current setting: `OFF`, `LOW`, `MEDIUM`, `HIGH` |
| `vest_state.capacity_remaining_pct` | Remaining cooling capacity (from the vest hardware) |
| `vest_state.laps_since_change` | Laps since the setting last changed (`next_vest_state` updates it) |

## Output

See `sample_output.json` for a full example.

**`decision`**

| Field | From | Meaning |
|---|---|---|
| `new_setting` | Gemini | Recommended setting for the next lap (may equal the current one) |
| `confidence` | Gemini | 0–1 |
| `reason` | Gemini | One short sentence (≤15 words) for the pop-up |
| `monitor` | Gemini | 3 short items to watch |
| `key_factors` | Gemini | 2–4 bullets citing the numbers used (for logs/debugging) |
| `lap`, `previous_setting` | code | Lap and current setting |
| `change_required` | code | `true` if `new_setting` differs from the current setting |
| `zone` | code | `CRITICAL_HEAT` / `GREEN_ZONE` / `COLD_SNAP`, from the heat index |

**`ui`** — ready for the pop-up (`HTML schemas/popup_ui.html`)

| `ui` field | Pop-up element |
|---|---|
| `zone` | Colour theme: red (`popup_ui.html`), cyan (`_cool`), blue (`_cold`) |
| `title` | `.title` (e.g. CRITICAL HEAT) |
| `status_text` | `.status` |
| `track_temp_c` | Gauge readout and "Track" |
| `air_temp_c`, `humidity_pct` | "Air", "Humidity" |
| `switch_to` | `.rec-value` |
| `from` | `.rec-current` ("From: …") |
| `reason` | `.rec-reason` |
| `monitor` | The three `.risk-item` rows |
| `accept_label`, `reject_label` | Button text; `null` when `change_required` is `false` |
| `change_required` | If `false`, show a "keep current setting" state without Accept/Reject |

## Where to change things

| To change | Edit in `cooling_vest_agent.py` |
|---|---|
| Prompt (role, rules, output) | `SYSTEM_PROMPT`, `USER_PROMPT_TEMPLATE` |
| Output fields | `VestDecision` (and the OUTPUT section of the prompt) |
| Zone thresholds | `ZONE_CRITICAL_HEAT_C`, `ZONE_COLD_SNAP_C` |
| Gemini model | `MODEL`, `BACKUP_MODEL` |
| Fallback rule | `rule_based_fallback()` |

## Notes

- Zone and fallback thresholds are **placeholders**, not validated values.
- `capacity_remaining_pct` is passed through unchanged; a real system would read it from the vest.
- Never commit your API key. `.env` and the FastF1 cache are in `.gitignore`.
