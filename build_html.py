"""
Static HTML generator for the parsed Fields of Mistria NPC schedules.

Reads `output/json/<npc>_schedule.json` (produced by parse_schedules.py)
and writes `output/html/index.html` plus one `output/html/<npc>.html`
per NPC.

Styling is loosely modeled on the aldarian-archive Vue app (dark theme,
NPC icons, season/weather icons), but the output is plain static HTML
that opens directly from disk -- no build step, no JS framework, no
fetch() needed.

GROUPING LOGIC
--------------
Each schedule entry is bucketed into (season, day):
  - season comes from the `season` constraint when present
  - day comes from `day_of_the_week` constraint(s); an OR group of days
    produces one bucket per day in the group
  - weather == "rainy"  -> bucket as the "rainy" pseudo-season
  - weather == "snowy"  -> bucket as winter (the old Vue app put these
    in spring by accident; fixing it here)
  - entries with no season AND no day go in an "any" bucket so they're
    still discoverable
  - quest / counter / festival / flag constraints become "extra
    conditions" displayed above the itinerary table
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
from typing import Any

DEFAULT_JSON_DIR    = r"C:\Code\schedule-parser\output\json"
DEFAULT_HTML_DIR    = r"C:\Code\schedule-parser\output\html"
DEFAULT_ASSETS_DIR  = r"C:\Code\schedule-parser\assets"

# All assets are bundled in the repo's assets/ folder and copied into
# output/html/assets/ at build time. No external/remote URLs -- the site
# is fully self-contained so it survives if aldarian-archive's branch is
# renamed or its assets repo goes away.
#
# Sources (for the curious):
#   - NPC / season / weather sprites (PNGs in assets/npc, /season, /weather):
#       Originally pulled from the aldarian-archive assets branch on GitHub
#       (https://github.com/AnnaNomoly/aldarian-archive, assets branch).
#       Those in turn came out of Fields of Mistria's data.win via
#       UndertaleModTool. The naming convention is the game's own:
#           spr_ui_generic_icon_npc_<name>_0.png
#           spr_ui_hud_info_backplate_season_icon_<spring|summer|autumn|winter>_0.png
#           spr_ui_hud_info_backplate_weather_icon_rainy_0.png
#       Each NPC icon is 24x20 with the portrait padded inside the bbox.
#   - title_logo.png, bg_dark.png: aldarian-archive UI assets.
#
# To add a new NPC icon when the game gets one (e.g. stillwell, zorel, or
# a future-patch NPC):
#   1. Run export_missing_npc_icons.csx in UMT against the current data.win.
#      Update the `npcs` array in that script first if needed.
#   2. Drop the resulting PNG into assets/npc/. Keep the game's filename
#      convention so it's clear the file came straight from data.win.
#   3. (If the file doesn't follow the spr_ui_generic_icon_npc_<lower>_0.png
#      pattern) add an entry to NPC_ICON_OVERRIDES below.
#   4. Commit; the GH Actions workflow rebuilds and redeploys.
NPC_ICON_OVERRIDES: dict[str, str | None] = {
    # NPCs without an in-game icon -- the card just shows the name.
    "stillwell": None,
    "zorel":     None,
}


def npc_icon_url(npc: str) -> str | None:
    """
    Returns the relative URL for an NPC's icon, or None if no icon is
    available (renderers should skip the <img> in that case).
    """
    if npc in NPC_ICON_OVERRIDES:
        override = NPC_ICON_OVERRIDES[npc]
        if override is None:
            return None
        # Allow custom filenames; default to the standard convention.
        return f"assets/npc/{override}"
    return f"assets/npc/spr_ui_generic_icon_npc_{npc.lower()}_0.png"

SEASON_ORDER = ["rainy", "spring", "summer", "fall", "winter", "any"]
DAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "any"]

# season name in URL differs from display name: fall -> autumn icon
SEASON_ICON_NAME = {
    "spring": "spring",
    "summer": "summer",
    "fall":   "autumn",
    "winter": "winter",
}


# ---------------------------------------------------------------------------
# constraint explanations (for tooltips)
# ---------------------------------------------------------------------------

# Keyed by raw_name. Plain-text only -- gets HTML-escaped into a data-tip
# attribute on a `(?)` element. Festival constraints are matched by suffix
# in explain_constraint() instead of listed individually here.
FLAG_EXPLANATIONS: dict[str, str] = {
    # --- Story-state flags ---
    "caldarus_home":
        "Whether Caldarus has reached the story state where he's living in Mistria. "
        "Story-permanent once unlocked.",
    "caldarus_seridia_town":
        "Whether Caldarus and Seridia are currently visiting town. The flag is "
        "true on visiting days and false otherwise.",
    "caldarus_counter":
        "Caldarus questline progression counter. Each integer step marks a story "
        "milestone that unlocks new schedule variants.",
    "seridia_counter":
        "Seridia questline progression counter. Each integer step marks a story "
        "milestone that unlocks new schedule variants.",
    "seridia_is_human":
        "Whether Seridia is currently in her human form (1.0) or dragon form (0.0).",
    "dragon_market":
        "Which dragon is running the Saturday market this week. The value is "
        "either 'seridia' or 'caldarus'.",
    "cutscene_seen_repair_the_summit_stairs":
        "Has the player seen the Repair the Summit Stairs cutscene? Story flag "
        "that gates post-cutscene schedule variants.",

    # --- Rotation counters (cycle through variants for variety) ---
    "fnati":
        "Friday Night At The Inn rotation index. The counter cycles through "
        "different Friday-evening events so the inn isn't identical every week.",
    "drawing_fnati":
        "Drawing-themed Friday Night At The Inn rotation index. Selects which "
        "drawing-class variant runs this week.",
    "dessert_fnati":
        "Dessert-themed Friday Night At The Inn rotation index. Selects which "
        "dessert variant runs this week.",
    "rain_counter":
        "Rainy-day rotation index. The counter cycles so rainy days aren't "
        "identical. Each value pairs NPCs with a different rainy schedule.",
    "rain_group_1":
        "Sub-grouping flag for the rainy-day rotation, alternating which group "
        "of NPCs uses which rainy schedule.",
    "summer_tuesday_progress":
        "Summer-Tuesday rotation index. Selects which summer-Tuesday schedule "
        "variant runs this week.",

    # --- Quests ---
    "quest_repair_the_bridge_complete":
        "Has the 'Repair the Bridge' quest been completed? Unlocks access to "
        "areas (and the vendors there) on the far side of the bridge.",
    "quest_upgrade_the_saturday_market_complete":
        "Has the 'Upgrade the Saturday Market' quest been completed? Expands "
        "what vendors and goods appear at the weekend market.",

    # --- Misc ---
    "museum_total_count":
        "Total number of items donated to the museum. Gates schedule entries "
        "that only fire once the museum reaches certain milestones.",
}


def explain_constraint(c: dict) -> str | None:
    """
    Return a human-readable explanation for a constraint, or None if there
    isn't one worth surfacing (basic calendar conditions are self-evident).
    """
    raw_name = c.get("raw_name")
    if not isinstance(raw_name, str):
        return None

    # Festival: <event>_festival_date  (matched by suffix so future festivals
    # work without code changes)
    if raw_name.endswith("_festival_date"):
        event = raw_name[: -len("_festival_date")].replace("_", " ").title()
        return f"Is today the {event} Festival? Triggers via a year-time comparison."

    # Quest fallback for any quest_*_complete that we don't have a specific
    # blurb for.
    if raw_name in FLAG_EXPLANATIONS:
        return FLAG_EXPLANATIONS[raw_name]
    if raw_name.startswith("quest_") and raw_name.endswith("_complete"):
        quest = raw_name[len("quest_"):-len("_complete")].replace("_", " ")
        return f"Has the '{quest}' quest been completed?"

    return None


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------

def render_constraint_inline(c: dict) -> str:
    """Render a single non-bucketing constraint for the 'extra conditions' line (plain text, no HTML)."""
    if c.get("kind") == "any":
        inner = " OR ".join(render_constraint_inline(x) for x in c["items"])
        return f"({inner})"
    label    = c.get("label", "?")
    comp     = (c.get("comparator") or "?").upper()
    val      = c.get("value_label", "?")
    priority = c.get("priority", 1)
    return f"[P{priority}] {label} {comp} {val}"


def render_constraint_html(c: dict) -> str:
    """HTML version of render_constraint_inline; adds a (?) tooltip if we have an explanation."""
    if c.get("kind") == "any":
        inner = " <span class=\"or\">OR</span> ".join(render_constraint_html(x) for x in c["items"])
        return f"<span class=\"or-group\">({inner})</span>"
    label    = c.get("label", "?")
    comp     = (c.get("comparator") or "?").upper()
    val      = c.get("value_label", "?")
    priority = c.get("priority", 1)
    base = f"[P{e(priority)}] {e(label)} {e(comp)} {e(val)}"
    tip = explain_constraint(c)
    if tip:
        # data-tip carries the explanation; the (?) is focusable so it works on mobile (tap to focus).
        return f"{base}<span class=\"info\" tabindex=\"0\" data-tip=\"{e(tip)}\" aria-label=\"What does this mean?\">?</span>"
    return base


def collect_days_from_any(any_group: dict) -> list[str] | None:
    """
    If every item in an Any group is a day_of_the_week constraint, return the
    list of day names. Otherwise return None (caller should treat as extra).
    """
    days = []
    for item in any_group.get("items", []):
        if item.get("kind") != "day_of_the_week":
            return None
        days.append(item["value_label"])
    return days if days else None


def classify_entry(entry: dict) -> dict:
    """
    Walk an entry's constraints and pull out the calendar-anchored facts
    (season / weather / day-of-week) plus everything else (extras).

    Also computes `base_score` = priority sum of season+weather+day
    constraints. This is the score the game uses to pick the most
    specific matching schedule when several would otherwise apply.
    """
    season: str | None = None
    season_not: str | None = None   # season != X (treated as extra; doesn't anchor a bucket)
    weather: str | None = None
    days_set: set[str] = set()
    days_explicit = False           # True if there is any day_of_week constraint
    extras: list[str] = []
    base_score = 0

    for c in entry.get("constraints", []):
        kind = c.get("kind")
        prio = c.get("priority", 1)

        if kind == "season":
            if c.get("comparator") == "Equal":
                season = c["value_label"]
                base_score += prio
            else:
                season_not = c["value_label"]
                extras.append(render_constraint_inline(c))

        elif kind == "weather":
            weather = c["value_label"]
            base_score += prio

        elif kind == "day_of_the_week":
            if c.get("comparator") == "Equal":
                days_set.add(c["value_label"])
                days_explicit = True
                base_score += prio
            else:
                extras.append(render_constraint_inline(c))

        elif kind == "any":
            collected_days = collect_days_from_any(c)
            if collected_days is not None:
                days_set.update(collected_days)
                days_explicit = True
                # The OR group contributes one match's worth of priority,
                # not one per alternative -- use the first item's priority.
                if c["items"]:
                    base_score += c["items"][0].get("priority", 1)
            else:
                extras.append(render_constraint_inline(c))

        else:
            # quest / counter / flag / festival / unknown
            extras.append(render_constraint_inline(c))

    return {
        "season":         season,
        "season_not":     season_not,
        "weather":        weather,
        "days_set":       days_set,
        "days_explicit":  days_explicit,
        "extras":         extras,
        "has_extras":     bool(extras),
        "base_score":     base_score,
    }


def _make_view(entry: dict, meta: dict) -> dict:
    return {
        "name":        entry["name"],
        "weather":     meta["weather"],
        "extras":      meta["extras"],
        "itinerary":   entry.get("itinerary", []),
        "base_score":  meta["base_score"],
        "conditional": meta["has_extras"],
        "constraints": entry.get("constraints", []),
    }


def _candidate_matches_cell(meta: dict, season: str, day: str) -> bool:
    """
    Returns True iff this entry's calendar constraints (season/day/weather)
    do not contradict the given cell. Extras are ignored -- they're treated
    as 'could be true at runtime'.
    """
    # Rainy entries belong only to the Rainy pseudo-season bucket.
    if meta["weather"] == "rainy":
        return False
    # Snowy weather is winter-only.
    if meta["weather"] == "snowy" and season != "winter":
        return False
    # season=X must match the cell; season!=X must not equal it.
    if meta["season"] is not None and meta["season"] != season:
        return False
    if meta["season_not"] is not None and meta["season_not"] == season:
        return False
    # day_of_week must include this day (if any day is specified).
    if meta["days_explicit"] and day not in meta["days_set"]:
        return False
    return True


def group_entries(entries: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """
    Cell-based bucketing that reflects how the game actually picks a
    schedule.

    For every (season, day) in sunny weather:
      1. Collect all entries whose calendar constraints are satisfiable
         in this cell.
      2. Find max base_score among UNCONDITIONAL candidates (no extras).
         These survive as the 'default fallback' for this cell.
      3. Any CONDITIONAL candidate with base_score >= the unconditional
         default survives too -- when its extras fire, it beats the
         default; when they don't, the default takes over.
      4. Lower-score unconditional candidates are strictly dominated --
         they would always lose to the max-score default -- so drop them.

    Special buckets:
      - 'rainy': all weather=rainy entries (no day split, mirrors the
        old Vue app and the game's Rainy Schedules folder).
      - 'any':   entries with NO season AND NO day AND NO weather
        constraints. These are state-only entries (e.g. caldarus/c_bed,
        darcy/basement_schedule) whose firing isn't anchored to the
        calendar at all.
    """
    classified = [(e, classify_entry(e)) for e in entries]

    result: dict[str, dict[str, list[dict]]] = {}

    # Rainy bucket -- weather=rainy entries, no day split.
    for entry, meta in classified:
        if meta["weather"] == "rainy":
            result.setdefault("rainy", {}).setdefault("any", []).append(_make_view(entry, meta))

    # "Any" bucket -- entries with no calendar anchoring of any kind.
    calendar_anchored = lambda m: (m["weather"] is not None or m["season"] is not None or m["days_explicit"])
    for entry, meta in classified:
        if not calendar_anchored(meta):
            result.setdefault("any", {}).setdefault("any", []).append(_make_view(entry, meta))

    # Sunny / snowy cells.
    seasons = ["spring", "summer", "fall", "winter"]
    days    = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    for s in seasons:
        for d in days:
            cands = [
                (e, m) for (e, m) in classified
                if calendar_anchored(m) and m["weather"] != "rainy" and _candidate_matches_cell(m, s, d)
            ]
            if not cands:
                continue

            uncond = [(e, m) for (e, m) in cands if not m["has_extras"]]
            if uncond:
                max_uncond = max(m["base_score"] for (_, m) in uncond)
                defaults = [(e, m) for (e, m) in uncond if m["base_score"] == max_uncond]
            else:
                max_uncond = -1
                defaults = []

            seen: set[str] = set()
            shown: list[tuple[dict, dict]] = []
            for e, m in defaults:
                if e["name"] not in seen:
                    shown.append((e, m))
                    seen.add(e["name"])
            for e, m in cands:
                if m["has_extras"] and m["base_score"] >= max_uncond and e["name"] not in seen:
                    shown.append((e, m))
                    seen.add(e["name"])

            # Sort: higher base_score first (most calendar-specific shown first).
            # Within equal score, conditional (gated) variants first so the
            # fallback default appears last.
            shown.sort(key=lambda em: (-em[1]["base_score"], 0 if em[1]["has_extras"] else 1))

            for e, m in shown:
                result.setdefault(s, {}).setdefault(d, []).append(_make_view(e, m))

    return result


# ---------------------------------------------------------------------------
# time formatting
# ---------------------------------------------------------------------------

def format_clock_12h(seconds: int) -> str:
    """Convert seconds-of-day to 12h time; mark next-day for values > 86400."""
    if seconds < 0:
        return "?"
    hh = seconds // 3600
    mm = (seconds % 3600) // 60
    next_day = ""
    if hh >= 24:
        hh -= 24
        next_day = " (next day)"
    period = "AM" if hh < 12 else "PM"
    display_h = hh % 12
    if display_h == 0:
        display_h = 12
    return f"{display_h}:{mm:02d} {period}{next_day}"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def e(s: Any) -> str:
    """HTML-escape, treating None as empty."""
    if s is None:
        return ""
    return html.escape(str(s))


SHARED_CSS = """
* { box-sizing: border-box; }
html, body {
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #e0e0e0;
    background-color: #1a1d23;
    background-image: url('assets/bg_dark.png');
    background-position: center;
    background-attachment: fixed;
    background-size: cover;
    min-height: 100vh;
}
a { color: #80cbc4; text-decoration: none; }
a:hover { text-decoration: underline; }

header.topbar {
    background: rgba(26, 29, 35, 0.92);
    border-bottom: 1px solid #2a2f38;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    position: sticky;
    top: 0;
    z-index: 10;
}
header.topbar img.logo { height: 36px; }
header.topbar .crumbs { color: #9aa5b1; font-size: 14px; flex: 1; }
header.topbar .version { color: #c5c7c9; font-size: 13px; }

main {
    max-width: 1100px;
    margin: 24px auto;
    padding: 0 20px;
}

/* Index page: NPC grid */
.npc-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 14px;
}
.npc-card {
    background: rgba(35, 38, 46, 0.92);
    border: 1px solid #2a2f38;
    border-radius: 6px;
    padding: 14px;
    text-align: center;
    transition: border-color 0.12s, transform 0.12s;
}
.npc-card:hover {
    border-color: #80cbc4;
    transform: translateY(-1px);
}
.npc-card a { color: #e0e0e0; display: block; }
.npc-card img.npc-icon {
    width: 48px;
    height: 40px;
    image-rendering: pixelated;
    image-rendering: crisp-edges;
    margin-bottom: 6px;
}
.npc-card .npc-icon-missing {
    width: 48px;
    height: 40px;
    margin: 0 auto 6px auto;
}
.npc-card .npc-name { font-weight: 600; text-transform: capitalize; }
.npc-card .entry-count { font-size: 12px; color: #9aa5b1; margin-top: 4px; }

/* NPC detail page */
.npc-header {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 24px;
}
.npc-header img.npc-icon {
    width: 64px;
    height: 56px;
    image-rendering: pixelated;
    image-rendering: crisp-edges;
}
.npc-header .npc-icon-missing {
    width: 64px;
    height: 56px;
}
.npc-header h1 { margin: 0; text-transform: capitalize; }
.npc-header .entry-count { color: #9aa5b1; font-size: 14px; }

details.season {
    background: rgba(35, 38, 46, 0.92);
    border: 1px solid #2a2f38;
    border-radius: 6px;
    margin-bottom: 12px;
}
details.season > summary {
    padding: 12px 16px;
    cursor: pointer;
    font-weight: 600;
    font-size: 16px;
    text-transform: capitalize;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 10px;
    user-select: none;
}
details.season > summary::-webkit-details-marker { display: none; }
details.season > summary::before {
    content: "▶";
    font-size: 11px;
    color: #80cbc4;
    transition: transform 0.12s;
}
details.season[open] > summary::before { transform: rotate(90deg); }
details.season > summary img {
    width: 24px;
    height: 24px;
    image-rendering: pixelated;
    image-rendering: crisp-edges;
}
details.season > .season-body {
    padding: 6px 16px 14px 16px;
}

details.day {
    border: 1px solid #353a44;
    border-radius: 4px;
    margin: 8px 0;
    background: rgba(28, 31, 38, 0.7);
}
details.day > summary {
    padding: 8px 12px;
    cursor: pointer;
    font-weight: 500;
    text-transform: capitalize;
    color: #cdd5df;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 8px;
}
details.day > summary::-webkit-details-marker { display: none; }
details.day > summary::before {
    content: "▶";
    font-size: 10px;
    color: #80cbc4;
    transition: transform 0.12s;
}
details.day[open] > summary::before { transform: rotate(90deg); }
details.day > .day-body { padding: 0 12px 12px 12px; }

.entry-block { margin-top: 12px; }
.entry-header {
    font-size: 12px;
    color: #9aa5b1;
    margin-bottom: 4px;
    font-family: monospace;
}
.extras {
    background: rgba(60, 47, 32, 0.45);
    border-left: 3px solid #d4a25a;
    padding: 6px 10px;
    margin: 6px 0;
    font-family: monospace;
    font-size: 13px;
    white-space: pre-wrap;
}

table.itin {
    width: 100%;
    border-collapse: collapse;
    margin: 6px 0;
    font-size: 14px;
}
table.itin th {
    background: #2a2f38;
    color: #cdd5df;
    text-align: left;
    padding: 6px 10px;
    font-weight: 600;
    border-bottom: 1px solid #3b414c;
}
table.itin td {
    padding: 5px 10px;
    border-bottom: 1px solid #262a32;
    vertical-align: top;
}
table.itin tr:last-child td { border-bottom: none; }
table.itin td.time { font-family: monospace; white-space: nowrap; color: #80cbc4; }
table.itin td.loc-id { color: #cdd5df; }
table.itin td.point { color: #9aa5b1; font-style: italic; }
table.itin td.actions { color: #d4a25a; font-size: 12px; }

.weather-tag {
    display: inline-block;
    background: #2a2f38;
    border-radius: 3px;
    padding: 1px 6px;
    margin-left: 6px;
    font-size: 11px;
    color: #9aa5b1;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.weather-tag.rainy  { background: #2c3e50; color: #b3d4fc; }
.weather-tag.snowy  { background: #2e3942; color: #e0e7ff; }
.weather-tag.sunny  { background: #3d3522; color: #f4d39b; }

.table-scroll {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
}

.extra-line { line-height: 1.5; }
.extra-line .or-group .or {
    color: #d4a25a;
    font-weight: 600;
    padding: 0 2px;
}

.info {
    display: inline-block;
    margin-left: 5px;
    width: 16px;
    height: 16px;
    line-height: 14px;
    text-align: center;
    font-size: 11px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #80cbc4;
    border: 1px solid #4d6c68;
    border-radius: 50%;
    cursor: help;
    position: relative;
    vertical-align: 1px;
    user-select: none;
}
.info:hover, .info:focus {
    color: #1a1d23;
    background: #80cbc4;
    border-color: #80cbc4;
    outline: none;
}
.info::after {
    content: attr(data-tip);
    position: absolute;
    bottom: calc(100% + 6px);
    left: 50%;
    transform: translateX(-50%);
    background: #0f1216;
    border: 1px solid #80cbc4;
    border-radius: 4px;
    padding: 8px 10px;
    width: max-content;
    max-width: 320px;
    white-space: normal;
    font-size: 12px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #e0e0e0;
    text-align: left;
    line-height: 1.4;
    pointer-events: none;
    opacity: 0;
    visibility: hidden;
    transition: opacity 0.12s, visibility 0.12s;
    z-index: 100;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
}
.info::before {
    content: "";
    position: absolute;
    bottom: 100%;
    left: 50%;
    transform: translateX(-50%);
    border: 5px solid transparent;
    border-top-color: #80cbc4;
    opacity: 0;
    visibility: hidden;
    transition: opacity 0.12s, visibility 0.12s;
    z-index: 100;
}
.info:hover::after, .info:focus::after,
.info:hover::before, .info:focus::before {
    opacity: 1;
    visibility: visible;
}

footer {
    margin: 36px auto 20px auto;
    text-align: center;
    color: #6b7280;
    font-size: 12px;
    padding: 0 20px;
}
footer p { margin: 6px 0; }

@media (max-width: 640px) {
    header.topbar {
        padding: 10px 12px;
        gap: 10px;
        flex-wrap: wrap;
    }
    header.topbar img.logo { height: 28px; }
    header.topbar .crumbs { font-size: 13px; flex-basis: 100%; order: 3; }
    header.topbar .version { font-size: 12px; }

    main { padding: 0 12px; margin: 16px auto; }

    .npc-grid {
        grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
        gap: 10px;
    }
    .npc-card { padding: 10px; }

    .npc-header { gap: 12px; margin-bottom: 16px; }
    .npc-header img.npc-icon { width: 48px; height: 42px; }
    .npc-header h1 { font-size: 22px; }

    details.season > summary { padding: 10px 12px; font-size: 15px; }
    details.season > .season-body { padding: 4px 10px 10px 10px; }
    details.day > summary { padding: 7px 10px; }
    details.day > .day-body { padding: 0 8px 10px 8px; }

    table.itin { font-size: 13px; }
    table.itin th, table.itin td { padding: 5px 8px; }
    table.itin td.actions { font-size: 11px; }

    .extras { font-size: 12px; }
    .entry-header { font-size: 11px; }

    .info::after { max-width: 240px; font-size: 11px; }
}
"""


def page_skeleton(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{e(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="style.css">
</head>
<body>
{body}
<footer>
<p>Copyright &copy; 2026 AnnaNomoly. Original content &amp; source code is licensed under the AGPL-3.0 license.</p>
<p>Fields of Mistria is trademark &amp; copyright of NPC Studios 2019&ndash;2026. This webpage is not affiliated with NPC Studios.</p>
</footer>
</body>
</html>
"""


def render_topbar(crumbs_html: str, version: str) -> str:
    return f"""
<header class="topbar">
  <img class="logo" src="assets/title_logo.png" alt="Aldarian Archive">
  <div class="crumbs">{crumbs_html}</div>
  <div class="version">{e(version)}</div>
</header>
"""


def render_index(npcs: list[tuple[str, int]], version: str) -> str:
    cards = []
    for npc, count in npcs:
        icon_url = npc_icon_url(npc)
        icon_img = (
            f'<img class="npc-icon" src="{icon_url}" alt="">'
            if icon_url
            else '<div class="npc-icon-missing" aria-hidden="true"></div>'
        )
        cards.append(f"""
        <div class="npc-card">
          <a href="{e(npc)}.html">
            {icon_img}
            <div class="npc-name">{e(npc)}</div>
            <div class="entry-count">{count} entries</div>
          </a>
        </div>""")
    body = render_topbar("<strong>Schedules</strong>", version) + f"""
<main>
  <h1 style="margin-top:0">NPC Schedules</h1>
  <p style="color:#9aa5b1">Select an NPC to view their schedule grouped by season and day of the week.</p>
  <div class="npc-grid">{"".join(cards)}
  </div>
</main>
"""
    return page_skeleton("NPC Schedules - Aldarian Archive", body)


def render_itinerary_table(itinerary: list[dict]) -> str:
    rows = []
    for it in itinerary:
        secs = it.get("time_seconds", -1)
        clock12 = format_clock_12h(secs) if isinstance(secs, int) else "?"
        actions = []
        if it.get("has_on_arrival_actions"):     actions.append("on_arrival")
        if it.get("has_on_departure_actions"):   actions.append("on_departure")
        if it.get("has_delayed_arrival_actions"):actions.append("delayed_arrival")
        rows.append(f"""<tr>
            <td class="time">{e(clock12)}</td>
            <td class="loc-id">{e(it.get('location_id'))}</td>
            <td class="point">{e(it.get('point_name'))}</td>
            <td class="actions">{e(', '.join(actions))}</td>
        </tr>""")
    return f"""<table class="itin">
        <thead><tr>
            <th>Departure</th><th>Location</th><th>Point</th><th>Actions</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>"""


def render_entry_block(view: dict) -> str:
    raw_constraints = view.get("constraints", [])
    weather_html = ""
    w = view.get("weather")
    if w:
        weather_html = f'<span class="weather-tag {e(w)}">{e(w)}</span>'

    # Re-render the extras from the raw constraint dicts so we can attach
    # tooltips. (The strings in view["extras"] are already escaped plain text.)
    extra_lines: list[str] = []
    for c in raw_constraints:
        kind = c.get("kind")
        if kind == "weather":
            continue  # shown as weather tag
        if kind == "season" and c.get("comparator") == "Equal":
            continue  # shown by season bucket placement
        if kind == "day_of_the_week" and c.get("comparator") == "Equal":
            continue  # shown by day bucket placement
        if kind == "any":
            # day-only OR groups are consumed by bucket placement; skip them
            if collect_days_from_any(c) is not None:
                continue
        extra_lines.append(render_constraint_html(c))

    extras_html = ""
    if extra_lines:
        rows = "".join(f'<div class="extra-line">{line}</div>' for line in extra_lines)
        extras_html = f'<div class="extras">{rows}</div>'

    return f"""<div class="entry-block">
        <div class="entry-header">{e(view['name'])}{weather_html}</div>
        {extras_html}
        <div class="table-scroll">{render_itinerary_table(view['itinerary'])}</div>
    </div>"""


def render_season_block(season: str, days_dict: dict[str, list[dict]]) -> str:
    # Order days
    ordered_days = [d for d in DAY_ORDER if d in days_dict]

    icon_html = ""
    if season == "rainy":
        icon_html = '<img src="assets/weather/spr_ui_hud_info_backplate_weather_icon_rainy_0.png" alt="">'
    elif season in SEASON_ICON_NAME:
        icon_html = f'<img src="assets/season/spr_ui_hud_info_backplate_season_icon_{SEASON_ICON_NAME[season]}_0.png" alt="">'

    season_label = season if season != "any" else "any-season"
    day_blocks = []
    for day in ordered_days:
        entry_blocks = [render_entry_block(v) for v in days_dict[day]]
        day_label = day if day != "any" else "any day"
        day_blocks.append(f"""<details class="day">
            <summary>{e(day_label)} ({len(days_dict[day])})</summary>
            <div class="day-body">{''.join(entry_blocks)}</div>
        </details>""")

    return f"""<details class="season">
        <summary>{icon_html}{e(season_label)}</summary>
        <div class="season-body">{''.join(day_blocks)}</div>
    </details>"""


def render_npc_page(npc: str, entries: list[dict], version: str) -> str:
    grouped = group_entries(entries)
    icon_url = npc_icon_url(npc)
    icon_img = (
        f'<img class="npc-icon" src="{icon_url}" alt="">'
        if icon_url
        else '<div class="npc-icon-missing" aria-hidden="true"></div>'
    )

    season_blocks = [
        render_season_block(s, grouped[s])
        for s in SEASON_ORDER
        if s in grouped
    ]

    crumbs = f'<a href="index.html">All NPCs</a> &rsaquo; <strong style="text-transform:capitalize">{e(npc)}</strong>'
    body = render_topbar(crumbs, version) + f"""
<main>
  <div class="npc-header">
    {icon_img}
    <div>
      <h1>{e(npc)}</h1>
      <div class="entry-count">{len(entries)} schedule entries</div>
    </div>
  </div>
  {''.join(season_blocks)}
</main>
"""
    return page_skeleton(f"{npc} - NPC Schedule", body)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Build static HTML pages from parsed NPC schedules.")
    ap.add_argument("--json-dir", default=DEFAULT_JSON_DIR,   help="Directory containing per-NPC JSON files")
    ap.add_argument("--out",      default=DEFAULT_HTML_DIR,   help="Output directory for HTML")
    ap.add_argument("--assets",   default=DEFAULT_ASSETS_DIR, help="Directory of local asset PNGs to copy into output/assets/")
    ap.add_argument("--version",  default="0.15.3",           help="Game version label for the header")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    # Write the shared stylesheet once.
    with open(os.path.join(args.out, "style.css"), "w", encoding="utf-8") as f:
        f.write(SHARED_CSS)

    # Copy local assets (NPC / season / weather icons, logo, background)
    # into output/assets/, preserving the subdirectory structure.
    if os.path.isdir(args.assets):
        dst_assets = os.path.join(args.out, "assets")
        if os.path.isdir(dst_assets):
            shutil.rmtree(dst_assets)
        shutil.copytree(args.assets, dst_assets)
        copied = sum(len(files) for _, _, files in os.walk(dst_assets))
        print(f"Copied {copied} asset file(s) from {args.assets} to {dst_assets}")

    # Load every NPC JSON file in the json directory.
    npc_files = [f for f in os.listdir(args.json_dir) if f.endswith("_schedule.json")]
    npc_files.sort()

    npc_summary: list[tuple[str, int]] = []
    for filename in npc_files:
        with open(os.path.join(args.json_dir, filename), "r", encoding="utf-8") as f:
            data = json.load(f)
        npc = data["npc"]
        entries = data["entries"]
        html_out = render_npc_page(npc, entries, args.version)
        with open(os.path.join(args.out, f"{npc}.html"), "w", encoding="utf-8") as f:
            f.write(html_out)
        npc_summary.append((npc, len(entries)))
        print(f"  {npc}.html ({len(entries)} entries)")

    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(npc_summary, args.version))
    print(f"\nWrote index.html and {len(npc_summary)} NPC pages to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
