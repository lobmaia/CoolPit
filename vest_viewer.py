"""
Cooling Vest Agent - single-call viewer

Runs ONE Gemini decision for one lap and writes an HTML report showing:
  1. Input data (race data for recent laps + vest state)
  2. Payload sent to Gemini (race context + vest state)
  3. System prompt / user prompt actually sent to Gemini
  4. Gemini output (decision JSON)
Then opens the report in your browser.

Usage:
    python vest_viewer.py                    # current_lap from sample_input.json
    python vest_viewer.py --lap 52           # a specific lap
    python vest_viewer.py --from-log         # re-render the last logged decision (no Gemini call)
    python vest_viewer.py --lap 52 --no-open # write the HTML without opening the browser

Each run writes vest_reports/vest_<driver>_lap<N>_<time>.html and also vest_reports/latest.html
(latest.html is the file to publish when sharing, so the shared link stays the same).
"""
import argparse
import html
import json
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import cooling_vest_agent as agent

REPORT_DIR = Path("./vest_reports")


def esc(text):
    return html.escape(str(text))


def pretty(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False, default=float)


def input_table(laps, lap, n=8):
    """Race data for the last n laps up to the decision lap (what the live feed would provide)."""
    rows = laps.loc[max(1, lap - n + 1):lap]
    cols = ["lap", "track_status", "air_temp_c", "track_temp_c", "humidity_pct", "heat_index_c",
            "tyre", "tyre_age", "lap_time_s", "pit_lap"]
    head = "".join(f'<th scope="col">{esc(c)}</th>' for c in cols)
    body = ""
    for n_lap, r in rows.iterrows():
        vals = [n_lap, agent.track_status_label(r["TrackStatus"]), round(r["AirTemp"], 1), round(r["TrackTemp"], 1),
                round(r["Humidity"], 1), round(r["HeatIndex"], 1), r["Compound"],
                int(r["TyreLife"]) if r["TyreLife"] == r["TyreLife"] else "",
                round(r["LapTimeS"], 3) if r["LapTimeS"] == r["LapTimeS"] else "", "yes" if r["PitLap"] else ""]
        cls = ' class="current"' if n_lap == lap else ""
        body += f"<tr{cls}>" + "".join(f"<td>{esc(v)}</td>" for v in vals) + "</tr>"
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def code_block(block_id, text):
    return (f'<div class="code"><button type="button" class="copy" data-target="{block_id}">Copy</button>'
            f'<pre id="{block_id}">{esc(text)}</pre></div>')


PAGE_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;700&family=Source+Sans+3:wght@400;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
  :root {
    --bg: #eef1f3; --surface: #ffffff; --surface-2: #e6eaee; --text: #151a20; --muted: #56616d;
    --line: #d3d9df; --accent: #c8102e; --ice: #0b69b7; --ice-bg: #dcebf8; --idle: #56616d; --idle-bg: #e2e6ea;
    --warn: #8a5a00; --warn-bg: #fff1d1; --hl: #fff4d6;
    --display: "Barlow Condensed", "Arial Narrow", sans-serif;
    --body: "Source Sans 3", "Segoe UI", "Malgun Gothic", system-ui, sans-serif;
    --mono: "JetBrains Mono", Consolas, ui-monospace, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --bg: #0f1317; --surface: #171c22; --surface-2: #1f262e; --text: #e5e9ed; --muted: #97a3af;
      --line: #2b333c; --accent: #ff4a5f; --ice: #6cb8ff; --ice-bg: #12304c; --idle: #a7b1bc; --idle-bg: #252c34;
      --warn: #ffc861; --warn-bg: #3a2c10; --hl: #37301a;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #0f1317; --surface: #171c22; --surface-2: #1f262e; --text: #e5e9ed; --muted: #97a3af;
    --line: #2b333c; --accent: #ff4a5f; --ice: #6cb8ff; --ice-bg: #12304c; --idle: #a7b1bc; --idle-bg: #252c34;
    --warn: #ffc861; --warn-bg: #3a2c10; --hl: #37301a;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font: 15px/1.55 var(--body); }
  main { max-width: 1100px; margin: 0 auto; padding-inline: 16px; padding-block: 24px 64px; display: grid; gap: 16px; }
  h1, h2 { font-family: var(--display); font-weight: 700; text-wrap: balance; margin: 0; }
  h1 { font-size: 30px; letter-spacing: 0.02em; }
  h1 .accent { color: var(--accent); }
  h2 { font-size: 20px; letter-spacing: 0.04em; text-transform: uppercase; display: flex; gap: 10px; align-items: baseline; }
  .sub { color: var(--muted); font-size: 13px; margin: 4px 0 0; }
  .notice { background: var(--warn-bg); color: var(--warn); border-radius: 8px; padding: 10px 14px; font-size: 14px; }
  .notice b { font-weight: 600; }
  section { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 18px; display: grid; gap: 12px; min-width: 0; }
  .step { font-family: var(--mono); font-size: 12px; color: var(--accent); }
  .hint { color: var(--muted); font-size: 13px; margin: 0; }
  .decision { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; }
  .badge { font-family: var(--display); font-size: 40px; font-weight: 700; letter-spacing: 0.06em; padding: 6px 24px; border-radius: 8px; }
  .badge.on { color: var(--ice); background: var(--ice-bg); }
  .badge.off { color: var(--idle); background: var(--idle-bg); }
  .facts { display: grid; gap: 2px; font-size: 14px; }
  .facts b { font-weight: 600; }
  .radio { border-left: 3px solid var(--accent); background: var(--surface-2); border-radius: 4px; padding: 10px 12px; margin: 0; }
  .radio span { font-family: var(--display); letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-size: 13px; margin-right: 8px; }
  ul { margin: 0; padding-left: 20px; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
  .stat { background: var(--surface-2); border-radius: 8px; padding: 8px 12px; }
  .stat span { display: block; color: var(--muted); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }
  .stat b { font-family: var(--mono); font-weight: 600; font-size: 15px; font-variant-numeric: tabular-nums; }
  .code { position: relative; min-width: 0; }
  pre { background: var(--surface-2); border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin: 0;
        overflow-x: auto; font: 12.5px/1.5 var(--mono); white-space: pre-wrap; word-break: break-word; color: var(--text); }
  .copy { position: absolute; top: 8px; right: 8px; font: 600 12px var(--body); padding: 3px 10px; border-radius: 6px;
          border: 1px solid var(--line); background: var(--surface); color: var(--text); cursor: pointer; }
  .copy:hover { border-color: var(--muted); }
  .copy:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  details { display: grid; gap: 8px; }
  summary { cursor: pointer; color: var(--muted); font-size: 13px; font-weight: 600; margin-bottom: 8px; }
  .table-wrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font: 12.5px var(--mono); font-variant-numeric: tabular-nums; }
  th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: right; white-space: nowrap; }
  th { color: var(--muted); font-weight: 600; font-family: var(--body); }
  th:first-child, td:first-child { text-align: left; }
  tr.current td { background: var(--hl); font-weight: 600; }
  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
  @media (max-width: 600px) { .badge { font-size: 30px; } h1 { font-size: 24px; } .grid2 { grid-template-columns: 1fr; } }
</style>
"""

PAGE_SCRIPT = """
<script>
document.querySelectorAll('.copy').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var pre = document.getElementById(btn.dataset.target);
    var done = function (label) { btn.textContent = label; setTimeout(function () { btn.textContent = 'Copy'; }, 1400); };
    var selectText = function () {
      var range = document.createRange(); range.selectNodeContents(pre);
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      done('Selected, press Ctrl+C');
    };
    try {
      navigator.clipboard.writeText(pre.innerText).then(function () { done('Copied'); }, selectText);
    } catch (e) { selectText(); }
  });
});
</script>
"""


def render(raw, laps, lap, total_laps, decision, ui, source, payload, user_prompt, elapsed, command):
    d = decision
    rc, vs = payload["race_context"], payload["vest_state"]
    vest_cls = "off" if d["new_setting"] == "OFF" else "on"
    badge = f"{d['previous_setting']} → {d['new_setting']}" if d["change_required"] else f"KEEP {d['new_setting']}"
    factors = "".join(f"<li>{esc(k)}</li>" for k in d["key_factors"])
    monitor = " · ".join(esc(m) for m in d["monitor"])
    meta = {k: raw[k] for k in ("driver", "year", "event", "current_lap", "vest_state")}
    latency = f"{elapsed:.1f}s" if elapsed is not None else "n/a"
    w = rc["weather"]

    stats = [
        ("Heat index", f"{w['heat_index_c']} °C ({w['heat_index_trend_per_lap_last5']:+}/lap)"),
        ("Air · humidity", f"{w['air_temp_c']} °C · {w['humidity_pct']}%"),
        ("Track temp", f"{w['track_temp_c']} °C"),
        ("Track · laps left", f"{rc['track_status']} · {rc['laps_remaining']}"),
        ("Capacity left", f"{vs.get('capacity_remaining_pct')}%"),
        ("Laps since change", f"{vs.get('laps_since_change')}"),
    ]
    stat_html = "".join(f'<div class="stat"><span>{esc(k)}</span><b>{esc(v)}</b></div>' for k, v in stats)

    return f"""<meta charset="utf-8">
<title>CoolPit Call Viewer</title>
{PAGE_STYLE}
<main>
  <header>
    <h1><span class="accent">CoolPit</span> Call Viewer</h1>
    <p class="sub">{esc(raw['year'])} {esc(raw['event'])} · {esc(raw['driver'])} · Lap {lap} of {total_laps} ·
      generated {datetime.now():%Y-%m-%d %H:%M}</p>
  </header>

  <p class="notice"><b>Prototype.</b> Historical FastF1 data for this race stands in for the live race feed.
    The vest state (setting, capacity) is example data. No biometric data is used.</p>

  <section aria-labelledby="h-result">
    <h2 id="h-result">Decision</h2>
    <div class="decision">
      <div class="badge {vest_cls}">VEST {esc(badge)}</div>
      <div class="facts">
        <div>Zone <b>{esc(ui['title'])}</b> · confidence <b>{d['confidence']:.2f}</b></div>
        <div>Needs engineer approval <b>{'yes' if d['change_required'] else 'no (keep current setting)'}</b></div>
        <div>Source <b>{esc(source)}</b> · latency <b>{latency}</b></div>
      </div>
    </div>
    <p class="radio"><span>Reason</span>{esc(d['reason'])}</p>
    <p class="radio"><span>Monitor</span>{monitor}</p>
    <ul>{factors}</ul>
    <div class="stats">{stat_html}</div>
  </section>

  <section aria-labelledby="h-cmd">
    <h2 id="h-cmd"><span class="step">00</span>Command</h2>
    <p class="hint">PowerShell command that produced this report.</p>
    {code_block('cmd', command)}
  </section>

  <section aria-labelledby="h-input">
    <h2 id="h-input"><span class="step">01</span>Input data</h2>
    <p class="hint">Race data for the last 8 laps, as a live feed would provide it. The decision lap is highlighted.</p>
    {input_table(laps, lap)}
    <details open><summary>Input JSON: driver, race, current lap, vest state</summary>{code_block('meta', pretty(meta))}</details>
  </section>

  <section aria-labelledby="h-payload">
    <h2 id="h-payload"><span class="step">02</span>Payload sent to Gemini</h2>
    <p class="hint">Computed by code: race context for the current lap (including heat index and its trend) and vest state.</p>
    <div class="grid2">
      {code_block('race', pretty(rc))}
      {code_block('vest', pretty(vs))}
    </div>
  </section>

  <section aria-labelledby="h-prompt">
    <h2 id="h-prompt"><span class="step">03</span>Prompt sent to Gemini</h2>
    <p class="hint">Model {esc(agent.MODEL)} (backup {esc(agent.BACKUP_MODEL)}) · temperature 0.2 · JSON schema output</p>
    <details open><summary>System prompt: role, input, rules, output</summary>{code_block('sys', agent.SYSTEM_PROMPT)}</details>
    <details open><summary>User prompt: data inserted into the template</summary>{code_block('user', user_prompt)}</details>
  </section>

  <section aria-labelledby="h-out">
    <h2 id="h-out"><span class="step">04</span>Gemini output</h2>
    <p class="hint">Gemini returns new_setting, confidence, reason, monitor and key_factors. The code adds lap,
      previous_setting, change_required and zone.</p>
    {code_block('out', pretty(d))}
  </section>

  <section aria-labelledby="h-ui">
    <h2 id="h-ui"><span class="step">05</span>Pop-up payload</h2>
    <p class="hint">What the pit-wall pop-up (HTML schemas/popup_ui*.html) receives. The engineer then accepts or rejects.</p>
    {code_block('ui', pretty(ui))}
  </section>
</main>
{PAGE_SCRIPT}"""


def write_report(raw, html_text, lap):
    REPORT_DIR.mkdir(exist_ok=True)
    out = REPORT_DIR / f"vest_{raw['driver']}_lap{lap}_{datetime.now():%H%M%S}.html"
    out.write_text(html_text, encoding="utf-8")
    (REPORT_DIR / "latest.html").write_text(html_text, encoding="utf-8")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="sample_input.json")
    parser.add_argument("--lap", type=int, help="lap to evaluate (default: current_lap in the JSON)")
    parser.add_argument("--from-log", action="store_true", help="re-render the last logged decision, no Gemini call")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        raw = json.load(f)

    print("⏳ Loading FastF1 race data...")
    session = agent.load_race(raw["year"], raw["event"])
    laps = agent.lap_table(session, raw["driver"])
    total_laps = int(session.total_laps or laps.index.max())

    if args.from_log:
        with open(agent.LOG_FILE, encoding="utf-8") as f:
            entry = json.loads(f.readlines()[-1])
        lap, payload, decision, source = entry["lap"], entry["payload"], entry["decision"], entry["source"]
        user_prompt, elapsed = agent.build_user_prompt(raw, payload), None
    else:
        lap = args.lap or raw["current_lap"]
        print(f"⏳ Calling Gemini for lap {lap}...")
        start = time.time()
        decision, source, payload, user_prompt = agent.decide(raw, laps, total_laps, lap)
        elapsed = time.time() - start
    ui = agent.to_ui_payload(decision, payload)

    command = f"python vest_viewer.py --input {args.input} --lap {lap}"
    html_text = render(raw, laps, lap, total_laps, decision, ui, source, payload, user_prompt, elapsed, command)
    out = write_report(raw, html_text, lap)

    print(f"✅ VEST {decision['previous_setting']} -> {decision['new_setting']} (source {source})")
    print(f"📄 Report: {out.resolve()}  (also saved as vest_reports/latest.html)")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
