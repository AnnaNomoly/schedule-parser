"""
Fields of Mistria NPC schedule parser.

Reads the game's `t2_output.json` and produces, for every NPC:
  - output/txt/<npc>_schedule.txt   human-readable dump
  - output/json/<npc>_schedule.json structured form for downstream tooling

Tested against Fields of Mistria 0.15.3.

USAGE:
    python parse_schedules.py
    python parse_schedules.py --t2 path/to/t2_output.json --out path/to/output

NOTES ON CONSTRAINT LABELING
----------------------------
The game encodes schedule constraints as `WorldFactCheck` objects with a
parameter name, a value, and a comparator. Some parameter names are
human-meaningful (weather, season, day_of_the_week); others are internal
game counters / boolean flags (fnati, caldarus_counter, dragon_market, ...).

For known counters and flags we emit a readable label in the .txt output
(e.g. "FRIDAY NIGHT AT THE INN (COUNTER) EQUAL 1.0"). Unknown ones are
emitted verbatim with an "UNKNOWN" tag so they're easy to grep when a new
game patch introduces a flag we don't recognize yet.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from typing import Any

DEFAULT_T2_PATH = r"C:\Code\Mistria\Game DATA\0.15.3\t2_output.json"
DEFAULT_OUT_DIR = r"C:\Code\schedule-parser\output"

SECONDS_PER_DAY = 86400

SEASON_BY_SECONDS = {
    0: "spring",
    2419200: "summer",
    4838400: "fall",
    7257600: "winter",
}

DAY_BY_SECONDS = {
    0: "monday",
    86400: "tuesday",
    172800: "wednesday",
    259200: "thursday",
    345600: "friday",
    432000: "saturday",
    518400: "sunday",
}

WEATHER_BY_TOKEN = {
    "pleasant": "sunny",
    "rainy": "rainy",
    "snowy": "snowy",
}

# Readable labels for known flag/counter parameters. Match the FNATI style
# of the original parser. The right-hand side is what shows up in the .txt
# output; the .json output keeps the raw flag name plus this label.
FLAG_LABELS: dict[str, str] = {
    "fnati":                                 "FRIDAY NIGHT AT THE INN (COUNTER)",
    "drawing_fnati":                         "DRAWING FRIDAY NIGHT AT THE INN (COUNTER)",
    "dessert_fnati":                         "DESSERT FRIDAY NIGHT AT THE INN (COUNTER)",
    "summer_tuesday_progress":               "SUMMER TUESDAY (COUNTER)",
    "rain_counter":                          "RAINY DAY (COUNTER)",
    "rain_group_1":                          "RAIN GROUP 1 (FLAG)",
    "caldarus_counter":                      "CALDARUS QUESTLINE (COUNTER)",
    "caldarus_home":                         "CALDARUS AT HOME (FLAG)",
    "caldarus_seridia_town":                 "CALDARUS/SERIDIA VISITING TOWN (FLAG)",
    "seridia_counter":                       "SERIDIA QUESTLINE (COUNTER)",
    "seridia_is_human":                      "SERIDIA HUMAN FORM (FLAG)",
    "dragon_market":                         "SATURDAY MARKET VENDOR",
    "museum_total_count":                    "MUSEUM DONATIONS (COUNTER)",
    "cutscene_seen_repair_the_summit_stairs":"CUTSCENE SEEN: REPAIR THE SUMMIT STAIRS",
}

# Festival parameter names use a suffix convention.
FESTIVAL_SUFFIX = "_festival_date"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def resolve_content(v: Any) -> Any:
    """
    Constraint name/value fields are wrapped as:
        [ { "Resolved": { "type": "string"|"real"|..., "content": <x> } } ]
    Pull out .content.
    """
    if isinstance(v, list) and v and isinstance(v[0], dict) and "Resolved" in v[0]:
        return v[0]["Resolved"].get("content")
    return v


def format_clock(seconds: int) -> str:
    """
    Game stores itinerary times in seconds-of-day. Values > 86400 mean the
    NPC's day rolls past midnight, so 90000s = 01:00 'next day'. We preserve
    that by displaying e.g. "25:00 (next day 1:00)" rather than wrapping.
    """
    base = datetime.timedelta(seconds=seconds)
    total_minutes = seconds // 60
    hh, mm = divmod(total_minutes, 60)
    if hh < 24:
        return f"{hh:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{hh:02d}:{mm:02d}:{seconds % 60:02d} (next day {(hh - 24):02d}:{mm:02d})"


# ---------------------------------------------------------------------------
# constraint parsing
# ---------------------------------------------------------------------------

def parse_world_fact_check(wfc: dict) -> dict:
    """
    Returns a structured representation of one WorldFactCheck:
        {
            "kind":         "weather" | "season" | "day_of_the_week"
                            | "counter" | "flag" | "quest" | "festival"
                            | "unknown",
            "raw_name":     "<parameter_one>",
            "raw_value":    <parameter_two>,
            "comparator":   "Equal" | "NotEqual" | "LessThan" | "GreaterThanOrEqual" | ...,
            "priority":     int,
            "subtract_from": str | None,
            # plus kind-specific human-readable fields:
            "label":        readable LHS,
            "value_label":  readable RHS,
        }
    """
    name = resolve_content(wfc.get("name"))
    value = resolve_content(wfc.get("value"))
    comparator = wfc.get("comparator")
    priority = wfc.get("priority_value", 1)
    subtract_from = wfc.get("subtract_from")

    result = {
        "raw_name": name,
        "raw_value": value,
        "comparator": comparator,
        "priority": priority,
        "subtract_from": subtract_from,
    }

    # Weather
    if name == "weather":
        result["kind"] = "weather"
        result["label"] = "WEATHER"
        result["value_label"] = WEATHER_BY_TOKEN.get(value, str(value))
        return result

    # Season
    if name == "season":
        result["kind"] = "season"
        result["label"] = "SEASON"
        result["value_label"] = SEASON_BY_SECONDS.get(value, f"<unknown season {value}>")
        return result

    # Day of week
    if name == "day_of_the_week":
        result["kind"] = "day_of_the_week"
        result["label"] = "DAY"
        result["value_label"] = DAY_BY_SECONDS.get(value, f"<unknown day {value}>")
        return result

    # Quests: quest_<name>_complete (boolean as 0.0/1.0)
    if isinstance(name, str) and name.startswith("quest_") and name.endswith("_complete"):
        quest_name = name[len("quest_"):-len("_complete")]
        result["kind"] = "quest"
        result["label"] = f"QUEST COMPLETION: {quest_name}"
        if value == 1.0 or value == 1:
            result["value_label"] = "complete"
        elif value == 0.0 or value == 0:
            result["value_label"] = "incomplete"
        else:
            result["value_label"] = str(value)
        return result

    # Festivals: <something>_festival_date with subtract_from = "year_time"
    # The check is "year_time - <festival_date> < 86400" i.e. "we're within
    # the day-of the festival". We surface the festival name and ignore the
    # arithmetic.
    if isinstance(name, str) and name.endswith(FESTIVAL_SUFFIX):
        festival_name = name[: -len(FESTIVAL_SUFFIX)]
        result["kind"] = "festival"
        result["label"] = f"FESTIVAL DAY: {festival_name}"
        result["value_label"] = "today"
        return result

    # Known counters / flags
    if isinstance(name, str) and name in FLAG_LABELS:
        # Heuristic: distinguish counters (numeric scalar) from flags (bool-ish 0/1)
        # purely cosmetically; the label already encodes the intent.
        result["kind"] = "counter" if "(COUNTER)" in FLAG_LABELS[name] else "flag"
        result["label"] = FLAG_LABELS[name]
        # dragon_market is the one string-valued flag; show its content quoted
        if isinstance(value, str):
            result["value_label"] = f'"{value}"'
        else:
            result["value_label"] = str(value)
        return result

    # Fallback
    result["kind"] = "unknown"
    result["label"] = f"UNKNOWN FLAG: {name}"
    result["value_label"] = repr(value)
    return result


def parse_constraint(c: Any) -> dict | None:
    """
    A 'requires' element is one of:
      { "WorldFactCheck": {...} }
      { "Any": [[ <constraint>, <constraint>, ... ]] }   <- OR group
      "Empty"                                            <- placeholder, seen in older patches
      False                                              <- placeholder, seen in 0.15.3 Any arrays
    """
    if c == "Empty" or c is False or c is None:
        return None

    if not isinstance(c, dict):
        return {"kind": "unknown_wrapper", "raw": repr(c)}

    if "WorldFactCheck" in c:
        return parse_world_fact_check(c["WorldFactCheck"])

    if "Any" in c:
        any_val = c["Any"]
        # Spec says Any is a 2d list and only index 0 is used.
        items: list = []
        if isinstance(any_val, list):
            outer = any_val[0] if any_val else []
            if isinstance(outer, list):
                for inner in outer:
                    parsed = parse_constraint(inner)
                    if parsed is not None:
                        items.append(parsed)
        return {"kind": "any", "items": items}

    # Unknown wrapper key.
    return {"kind": "unknown_wrapper", "raw_keys": list(c.keys())}


# ---------------------------------------------------------------------------
# itinerary parsing
# ---------------------------------------------------------------------------

def parse_itinerary(itinerary: dict) -> list[dict]:
    out: list[dict] = []
    for time_key, event in itinerary.items():
        try:
            t = int(time_key)
        except (TypeError, ValueError):
            t = -1
        dest = event.get("destination") or {}
        out.append({
            "time_seconds": t,
            "clock": format_clock(t) if t >= 0 else str(time_key),
            "location_id": dest.get("location_id"),
            "point_name": dest.get("point_name"),
            "has_on_arrival_actions":  bool(event.get("on_arrival_actions")),
            "has_on_departure_actions":bool(event.get("on_departure_actions")),
            "has_delayed_arrival_actions": bool(event.get("delayed_arrival_actions")),
            "has_on_arrival_writes":   bool(event.get("on_arrival_writes")),
            "has_on_departure_writes": bool(event.get("on_departure_writes")),
        })
    out.sort(key=lambda x: x["time_seconds"])
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def render_constraint_inline(parsed: dict) -> str:
    """One readable line for a single constraint (used inside Any expressions and the top-level CONDITION lines)."""
    kind = parsed.get("kind")
    if kind == "any":
        # Recursively render an Any group as "(a OR b OR c)"
        inner = " OR ".join(render_constraint_inline(p) for p in parsed["items"])
        return f"({inner})"
    if kind in {"weather", "season", "day_of_the_week"}:
        return f"[P{parsed['priority']}] {parsed['label']} {parsed['comparator'].upper()} {parsed['value_label']}"
    if kind in {"counter", "flag", "quest", "festival"}:
        return f"[P{parsed['priority']}] {parsed['label']} {parsed['comparator'].upper()} {parsed['value_label']}"
    if kind == "unknown":
        return (f"[P{parsed['priority']}] {parsed['label']} "
                f"{parsed['comparator'].upper() if parsed['comparator'] else '?'} {parsed['value_label']}")
    if kind == "unknown_wrapper":
        return f"<UNKNOWN WRAPPER {parsed.get('raw_keys') or parsed.get('raw')}>"
    return f"<UNHANDLED {parsed}>"


def render_npc_text(npc: str, entries: list[dict]) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"NPC: {npc}")
    lines.append(f"Entries: {len(entries)}")
    lines.append("=" * 72)
    lines.append("")

    for entry in entries:
        lines.append(f"# {npc}/{entry['name']}")
        if not entry["constraints"]:
            lines.append("CONDITION: <none>")
        for c in entry["constraints"]:
            lines.append(f"CONDITION: {render_constraint_inline(c)}")

        lines.append("")
        lines.append("Itinerary:")
        if not entry["itinerary"]:
            lines.append("  <empty>")
        for it in entry["itinerary"]:
            extras = []
            if it["has_on_arrival_actions"]:    extras.append("on_arrival")
            if it["has_on_departure_actions"]:  extras.append("on_departure")
            if it["has_delayed_arrival_actions"]:extras.append("delayed_arrival")
            extras_str = f"  [actions: {', '.join(extras)}]" if extras else ""
            lines.append(
                f"  {it['clock']:>32s}  ->  {it['location_id']}({it['point_name']}){extras_str}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_one_npc(npc: str, raw_schedules: dict) -> list[dict]:
    parsed_entries: list[dict] = []
    for entry_name, entry in raw_schedules.items():
        constraints = []
        for req in entry.get("requires", []):
            parsed = parse_constraint(req)
            if parsed is not None:
                constraints.append(parsed)
        itinerary = parse_itinerary(entry.get("itinerary", {}))
        parsed_entries.append({
            "name": entry_name,
            "constraints": constraints,
            "itinerary": itinerary,
        })
    # Stable order: sort by entry name so diffs across game versions are readable.
    parsed_entries.sort(key=lambda e: e["name"])
    return parsed_entries


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse Fields of Mistria NPC schedules from t2_output.json.")
    ap.add_argument("--t2",  default=DEFAULT_T2_PATH, help="Path to t2_output.json")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR,  help="Output directory")
    args = ap.parse_args()

    print(f"Loading {args.t2} ...")
    with open(args.t2, "r", encoding="utf-8") as f:
        data = json.load(f)

    schedules = data["schedules"]
    print(f"Found {len(schedules)} NPCs.")

    txt_dir  = os.path.join(args.out, "txt")
    json_dir = os.path.join(args.out, "json")
    os.makedirs(txt_dir,  exist_ok=True)
    os.makedirs(json_dir, exist_ok=True)

    unknowns: set[str] = set()

    for npc in sorted(schedules):
        entries = parse_one_npc(npc, schedules[npc])

        # Collect unknown flags for end-of-run report.
        for entry in entries:
            for c in entry["constraints"]:
                if c.get("kind") == "unknown":
                    unknowns.add(str(c.get("raw_name")))
                elif c.get("kind") == "any":
                    for inner in c["items"]:
                        if inner.get("kind") == "unknown":
                            unknowns.add(str(inner.get("raw_name")))

        with open(os.path.join(txt_dir, f"{npc}_schedule.txt"), "w", encoding="utf-8") as f:
            f.write(render_npc_text(npc, entries))

        with open(os.path.join(json_dir, f"{npc}_schedule.json"), "w", encoding="utf-8") as f:
            json.dump({"npc": npc, "entries": entries}, f, indent=2)

        print(f"  {npc}: {len(entries)} entries")

    print()
    if unknowns:
        print("Unknown flag names encountered (consider adding to FLAG_LABELS):")
        for u in sorted(unknowns):
            print(f"  - {u}")
    else:
        print("No unknown flags encountered.")
    print(f"\nWrote output to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
