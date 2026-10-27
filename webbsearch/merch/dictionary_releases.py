"""Search-dictionary changes that ship with a release, applied once to each copy's dictionary (v2.0).

The dictionary lives in each copy's data folder (data/merch/dictionary.json: the Mac, the jump drive, the public
demo), so a release can't change it by editing a file in the repo. Each release's entries are applied at start-up the
first time a copy runs it, recorded in the dictionary's change log as "release <n>" and marked done in
dictionary_releases.json, so a merchandiser who later switches one off keeps it off.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from webbsearch.merch.dictionary import key

# (term, kind, to, none-words, note): live entries. Terms found by the Oct 22 contractor probes
# (docs/SEARCH-GAPS-2026-10-22.md); targets are the catalog's own words.
_LIVE_2_0 = [
    ("teflon tape", "synonym", "PTFE tape", [], "Titles say PTFE Tape."),
    ("thread tape", "synonym", "PTFE tape", [], "Titles say PTFE Tape."),
    ("plumbers tape", "synonym", "PTFE tape", [], "Titles say PTFE Tape."),
    ("pipe dope", "synonym", "pipe thread sealant", [], "Titles say Pipe Thread Sealant."),
    ("ballcock", "synonym", "fill valve", [], "Old trade name for a toilet fill valve."),
    ("ball cock", "synonym", "fill valve", [], "Old trade name for a toilet fill valve."),
    ("cart", "synonym", "cartridge", [], "Counter shorthand for a faucet or circulator cartridge."),
    ("copper pipe", "synonym", "copper tube", [], "Copper is listed as tube (Type K, L, M)."),
    ("type l pipe", "synonym", "type l copper tube", [], "Copper is listed as tube."),
    ("type k pipe", "synonym", "type k copper tube", [], "Copper is listed as tube."),
    ("type m pipe", "synonym", "type m copper tube", [], "Copper is listed as tube."),
    ("pro press", "synonym", "press", ["tool", "tools", "jaw", "jaws", "kit", "ring", "actuator"],
     "ProPress fittings are listed as Press; Pro Press tools keep their name."),
    ("bell reducer", "synonym", "reducing coupling", [], "A bell reducer is a reducing coupling."),
]
# (term, kind): entries to switch off. "T&P valve" -> "temperature and pressure relief valve" rewrote the catalog's own
# word (titles say T&P Relief Valve) into words no title has
_OFF_2_0 = [("T&P valve", "synonym")]

# v2.44: the review backlog. Every entry in review or off was measured on the catalog (scripts/qc_dictionary.py's
# Measurer: the typed words and the target each searched, share of the first 10 results carrying the target's words)
# and read; these 85 were approved by Claude at Brendan's request (Oct 25 2026), as the second person. Excluded on
# reading, though they measured well: rewrites of the catalog's own words (1/4 bend, FIP, MIP, FTG), ambiguous terms
# (IPS, end cap, sub pump, supply line), brand-wide rewrites that drop the product noun (B&G pump), entries the brand
# master now handles (fernco, Honeywell aquastat) and ones that drop a qualifier the note says to keep (sectional).
_APPROVE_2_44 = [
    ('22 refrigerant', 'synonym', 'R-22 refrigerant', 'was off; typed 205 results, 0% on target; as the target 245, 100%'),
    ('32 refrigerant', 'synonym', 'R-32 refrigerant', 'was off; typed 234 results, 0% on target; as the target 245, 100%'),
    ('410a', 'synonym', 'R-410A', 'was off; typed 155 results, 0% on target; as the target 525, 100%'),
    ('410A refrigerant', 'synonym', 'R-410A refrigerant', 'was off; typed 12 results, 10% on target; as the target 245, 100%'),
    ('454B refrigerant', 'synonym', 'R-454B refrigerant', 'was off; typed 12 results, 40% on target; as the target 244, 100%'),
    ('air conditioning unit', 'synonym', 'air conditioner', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('air eliminator', 'synonym', 'air separator', 'was off; typed 0 results, 0% on target; as the target 320, 100%'),
    ('ball shut off valve', 'synonym', 'ball valve', 'was off; typed 553 results, 0% on target; as the target 548, 100%'),
    ('baseboard cabinet', 'synonym', 'baseboard enclosure', 'was review; typed 0 results, 0% on target; as the target 72, 100%'),
    ('bath overflow plate', 'synonym', 'tub overflow cover', 'was off; typed 0 results, 0% on target; as the target 454, 67%'),
    ('bleed valve', 'synonym', 'manual radiator air vent', 'was review; typed 0 results, 0% on target; as the target 523, 75%'),
    ('blower cage', 'synonym', 'blower wheel', 'was review; typed 0 results, 0% on target; as the target 600, 100%'),
    ('blower relay', 'synonym', 'fan relay', 'was off; typed 454 results, 50% on target; as the target 420, 100%'),
    ('cast iron rad', 'synonym', 'cast iron radiator', 'was off; typed 0 results, 0% on target; as the target 600, 67%'),
    ('CI pipe', 'synonym', 'cast iron pipe', 'was off; typed 0 results, 0% on target; as the target 40, 100%'),
    ('combination gas valve', 'synonym', 'gas control valve', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('combustion tester', 'synonym', 'combustion analyzer', 'was off; typed 0 results, 0% on target; as the target 476, 100%'),
    ('commode', 'synonym', 'toilet', 'was review; typed 0 results, 0% on target; as the target 300, 100%'),
    ('CPVC glue', 'synonym', 'CPVC solvent cement', 'was review; typed 0 results, 0% on target; as the target 189, 67%'),
    ('crimping tool for PEX', 'synonym', 'PEX crimp tool', 'was review; typed 0 results, 0% on target; as the target 600, 100%'),
    ('ducted mini split', 'synonym', 'concealed ducted indoor unit', 'was off; typed 600 results, 33% on target; as the target 600, 67%'),
    ('FA adapter', 'synonym', 'female adapter', 'was review; typed 294 results, 55% on target; as the target 553, 100%'),
    ('fan switching relay', 'synonym', 'fan relay', 'was off; typed 454 results, 50% on target; as the target 420, 100%'),
    ('faucet cart', 'synonym', 'faucet cartridge', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('FD', 'synonym', 'floor drain', 'was review; typed 341 results, 0% on target; as the target 600, 100%'),
    ('flushometer valve', 'synonym', 'flushometer', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('forty five elbow', 'synonym', '45 degree elbow', 'was off; typed 0 results, 0% on target; as the target 510, 100%'),
    ('gas cock', 'synonym', 'gas valve', 'was off; typed 0 results, 0% on target; as the target 474, 100%'),
    ('gas-fired warm air furnace', 'synonym', 'gas furnace', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('gate shut off valve', 'synonym', 'gate valve', 'was off; typed 543 results, 0% on target; as the target 533, 100%'),
    ('gravity flow check', 'synonym', 'flow-control check valve', 'was off; typed 0 results, 0% on target; as the target 454, 67%'),
    ('hand held shower', 'synonym', 'hand shower', 'was off; typed 0 results, 0% on target; as the target 572, 100%'),
    ('handheld shower', 'synonym', 'hand shower', 'was off; typed 0 results, 0% on target; as the target 572, 100%'),
    ('ion exchange softener', 'synonym', 'water softener', 'was off; typed 0 results, 0% on target; as the target 454, 100%'),
    ('janitor sink', 'synonym', 'service sink', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('janitorial sink', 'synonym', 'service sink', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('laundry box', 'synonym', 'washing machine outlet box', 'was off; typed 0 results, 0% on target; as the target 454, 100%'),
    ('laundry sink', 'synonym', 'utility sink', 'was off; typed 0 results, 0% on target; as the target 578, 100%'),
    ('lav', 'synonym', 'lavatory', 'was off; typed 600 results, 50% on target; as the target 324, 100%'),
    ('LLFD', 'synonym', 'liquid-line filter-drier', 'was review; typed 0 results, 0% on target; as the target 350, 67%'),
    ('long turn 90', 'synonym', 'long sweep 90 degree elbow', 'was off; typed 0 results, 0% on target; as the target 437, 67%'),
    ('low water cut-off', 'synonym', 'low-water cutoff', 'was off; typed 0 results, 0% on target; as the target 600, 75%'),
    ('modcon boiler', 'synonym', 'modulating condensing boiler', 'was review; typed 0 results, 0% on target; as the target 454, 67%'),
    ('moisture indicator sight glass', 'synonym', 'refrigerant sight glass', 'was off; typed 0 results, 0% on target; as the target 454, 67%'),
    ('NH', 'synonym', 'no hub', 'was review; typed 455 results, 0% on target; as the target 40, 100%'),
    ('ninety', 'synonym', '90 degree elbow', 'was off; typed 0 results, 0% on target; as the target 544, 100%'),
    ('ninety elbow', 'synonym', '90 degree elbow', 'was off; typed 0 results, 0% on target; as the target 544, 100%'),
    ('overflow plate', 'synonym', 'tub overflow cover', 'was off; typed 0 results, 0% on target; as the target 454, 67%'),
    ('PEX pinch tool', 'synonym', 'PEX clamp tool', 'was off; typed 0 results, 0% on target; as the target 440, 67%'),
    ('PVC glue', 'synonym', 'PVC solvent cement', 'was review; typed 0 results, 0% on target; as the target 252, 67%'),
    ('radiator thermostatic valve', 'synonym', 'thermostatic radiator valve', 'was off; typed 146 results, 0% on target; as the target 600, 100%'),
    ('recovery unit', 'synonym', 'refrigerant recovery machine', 'was off; typed 0 results, 0% on target; as the target 454, 67%'),
    ('reduced pressure principle backflow preventer', 'synonym', 'reduced pressure zone assembly', 'was off; typed 0 results, 0% on target; as the target 150, 100%'),
    ('running capacitor', 'synonym', 'motor run capacitor', 'was review; typed 0 results, 0% on target; as the target 560, 67%'),
    ('sched 40', 'synonym', 'sch 40', 'was off; typed 0 results, 0% on target; as the target 454, 100%'),
    ('sched 80', 'synonym', 'sch 80', 'was off; typed 0 results, 0% on target; as the target 454, 100%'),
    ('septic effluent pump', 'synonym', 'effluent pump', 'was off; typed 0 results, 0% on target; as the target 550, 100%'),
    ('sewer ejector', 'synonym', 'sewage ejector pump', 'was off; typed 0 results, 0% on target; as the target 565, 100%'),
    ('sewer grinder pump', 'synonym', 'grinder pump', 'was off; typed 0 results, 0% on target; as the target 565, 100%'),
    ('sink supply hose', 'synonym', 'faucet supply connector', 'was off; typed 0 results, 0% on target; as the target 360, 67%'),
    ('Sloane', 'brand', 'Sloan', 'was off; typed 0 results, 0% on target; as the target 717, 100%'),
    ('spring loaded check', 'synonym', 'spring check valve', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('spud washer for toilet tank', 'synonym', 'tank-to-bowl gasket', 'was off; typed 0 results, 0% on target; as the target 600, 75%'),
    ('squirrel cage wheel', 'synonym', 'blower wheel', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('starter collar', 'synonym', 'starting collar', 'was off; typed 0 results, 0% on target; as the target 288, 100%'),
    ('steamer boiler', 'synonym', 'steam boiler', 'was review; typed 0 results, 0% on target; as the target 576, 100%'),
    ('step down transformer', 'synonym', 'control transformer', 'was off; typed 0 results, 0% on target; as the target 454, 100%'),
    ('street ninety', 'synonym', '90 degree street elbow', 'was off; typed 0 results, 0% on target; as the target 454, 100%'),
    ('sump pit pump', 'synonym', 'sump pump', 'was off; typed 0 results, 0% on target; as the target 598, 100%'),
    ('tank bowl donut', 'synonym', 'tank-to-bowl gasket', 'was off; typed 0 results, 0% on target; as the target 600, 75%'),
    ('tap', 'synonym', 'faucet', 'was review; typed 0 results, 0% on target; as the target 600, 100%'),
    ('thermostatic tempering valve', 'synonym', 'thermostatic mixing valve', 'was off; typed 0 results, 0% on target; as the target 320, 100%'),
    ('toilet flex', 'synonym', 'toilet supply connector', 'was review; typed 461 results, 0% on target; as the target 180, 67%'),
    ('trap seal primer', 'synonym', 'trap primer', 'was off; typed 0 results, 0% on target; as the target 600, 100%'),
    ('tub drain assembly', 'synonym', 'bath waste and overflow', 'was off; typed 0 results, 0% on target; as the target 454, 67%'),
    ('valve stem for faucet', 'synonym', 'faucet stem', 'was off; typed 454 results, 50% on target; as the target 600, 100%'),
    ('vic coupling', 'synonym', 'grooved coupling', 'was off; typed 0 results, 0% on target; as the target 288, 100%'),
    ('washer box', 'synonym', 'washing machine outlet box', 'was review; typed 0 results, 0% on target; as the target 454, 100%'),
    ('water closet', 'synonym', 'toilet', 'was review; typed 600 results, 0% on target; as the target 300, 100%'),
    ('water filter canister', 'synonym', 'filter housing', 'was review; typed 0 results, 0% on target; as the target 454, 100%'),
    ('WC', 'synonym', 'toilet', 'was review; typed 600 results, 0% on target; as the target 454, 100%'),
    ('well x trol', 'synonym', 'well tank', 'was off; typed 0 results, 0% on target; as the target 440, 100%'),
]
# new live entries: product names the catalog has under other words (the Oct 25 trade-vocabulary probes)
_LIVE_2_44 = [
    ("auto air vent", "synonym", "air vent", [], "Titles say Air Vent (radiator, main and float vents)."),
    ("automatic air vent", "synonym", "air vent", [], "Titles say Air Vent."),
    ("plastic pipe glue", "synonym", "solvent cement", [], "Plastic is PVC, CPVC or ABS: all the solvent cements."),
    ("acr", "synonym", "ACR copper tube", ["drier", "dryer", "filter", "line", "set", "fitting", "fittings", "elbow",
                                           "coupling", "tee", "adapter"], "ACR alone is refrigeration copper tube."),
    ("lav supply", "synonym", "faucet supply", [], "Titles say Faucet Supply."),
    ("sink supply", "synonym", "faucet supply", [], "Titles say Faucet Supply."),
    ("toilet supply line", "synonym", "toilet supply", [], "Titles say Toilet Supply."),
    ("no hub band", "synonym", "no-hub coupling", [], "A no-hub band is the no-hub coupling."),
    ("drop ear 90", "synonym", "drop ear elbow", [], "A drop ear 90 is a drop ear elbow."),
    ("test cap", "synonym", "cap", [], "Knock-out test caps are listed as caps here; check before ordering."),
    # replaces "shock arrestor" -> "water hammer arrestor" (off): titles spell it Arrester, and a replacement isn't
    # re-matched by the arrestor -> arrester spelling entry
    ("shock arrestor", "synonym", "water hammer arrester", [], "A shock arrestor is a water hammer arrester."),
    ("shock arrester", "synonym", "water hammer arrester", [], "A shock arrester is a water hammer arrester."),
    # v2.44, from the trade vocabulary bank (scripts/qa/banks/trade-vocabulary-bank-2026-10-25.tsv)
    ("shower valve", "synonym", "pressure balance valve", [], "A shower valve is listed as a pressure balance valve body."),
    ("quarter turn stop", "synonym", "angle stop", [], "Quarter-turn stops here are angle stops."),
    ("j bend", "synonym", "p trap", [], "A J-bend is the trap's bend; traps are listed as P-traps."),
    ("j-bend", "synonym", "p trap", [], "A J-bend is the trap's bend; traps are listed as P-traps."),
    ("all thread", "synonym", "threaded rod", [], "All-thread is threaded rod."),
    ("fin tube", "synonym", "baseboard element", [], "Fin-tube is the baseboard element."),
    ("drain snake", "synonym", "drain cleaning machine", [], "A drain snake is a drain cleaning machine here."),
    ("sewer snake", "synonym", "drain cleaning machine", [], "A sewer snake is a drain cleaning machine here."),
    ("purple primer", "synonym", "PVC primer", [], "Purple primer is PVC primer (the color shows it was used)."),
    ("lineset", "synonym", "line set", [], "Lineset is a line set."),
    ("boiler fill valve", "synonym", "boiler feed valve", [], "A boiler fill valve is a feed valve, not a toilet fill valve."),
    ("isolation valve", "synonym", "isolation flange", [], "A circulator's isolation valve is an isolation flange.",
     ["circulator", "circulators", "circ", "pump"]),
    # v2.44, mined from fwwebb.com searches the demo answered weakly (scripts/vocab/mine_vocab.py), reviewed
    ("air scoop", "synonym", "air separator", [], "B&G's Air Scoop is an air separator."),
    ("blue monster", "synonym", "PTFE tape", [], "Blue Monster (Mill-Rose) is PTFE thread seal tape."),
    ("hot water heater", "synonym", "water heater", [], "A hot water heater is a water heater."),
    ("hepex", "synonym", "PEX oxygen barrier tubing", [], "Uponor hePEX is oxygen-barrier PEX-a."),
    ("slimduct", "synonym", "line set cover kit", [], "SlimDuct (Inaba Denko) is a line set cover."),
    ("slim duct", "synonym", "line set cover kit", [], "SlimDuct (Inaba Denko) is a line set cover."),
    ("line hide", "synonym", "line set cover kit", [], "LineHide is a line set cover."),
    ("silcock", "spelling", "sillcock", [], "Common spelling of sillcock."),
    ("circuit setter", "synonym", "balancing valve", [], "B&G's Circuit Setter is a balancing valve."),
    ("pex manifold", "synonym", "radiant manifold", [], "The PEX manifolds here (Uponor, Rehau, Watts, Caleffi) are listed as radiant manifolds."),
    ("port", "synonym", "loop", [], "On a manifold, a port is a loop (titles say 4 Loop).", ["manifold", "manifolds"]),
]
_OFF_2_44: list = []

RELEASES = [("2.0", _LIVE_2_0, _OFF_2_0), ("2.44", _LIVE_2_44, _OFF_2_44)]
APPROVALS = {"2.44": _APPROVE_2_44}


def _approved_entries(rel: str) -> dict:
    """config/dictionary/approved-<rel>.json: the approved entries in full (context words, definition)."""
    p = Path(__file__).resolve().parents[2] / "config" / "dictionary" / f"approved-{rel}.json"
    try:
        es = json.loads(p.read_text()).get("entries", []) if p.exists() else []
    except (json.JSONDecodeError, OSError):
        es = []
    return {(key(e["term"]), e["kind"], key(e.get("to") or "")): e for e in es}


def _mined_entries(rel: str) -> list[dict]:
    """config/dictionary/mined-*.json for a release: candidates from searches the demo answered weakly."""
    if rel != "2.44":
        return []
    p = Path(__file__).resolve().parents[2] / "config" / "dictionary" / "mined-2026-10-25.json"
    try:
        return [e for e in json.loads(p.read_text()).get("entries", []) if e.get("status") == "review"]
    except (OSError, json.JSONDecodeError):
        return []


def apply_releases(dic, root: Path | str, log=lambda *_: None) -> list[str]:
    """Apply each release's dictionary changes once. Returns the releases applied now."""
    marker = Path(root) / "dictionary_releases.json"
    try:
        done = set(json.loads(marker.read_text()).get("applied", [])) if marker.exists() else set()
    except (json.JSONDecodeError, OSError):
        done = set()
    applied = []
    for rel, live, off in RELEASES:
        if rel in done:
            continue
        who = f"release {rel}"
        n = 0
        for row in live:
            term, kind, to, none, note = row[:5]
            any_ = row[5] if len(row) > 5 else []
            k = key(term)
            same = [e for e in dic.entries() if key(e["term"]) == k and e["kind"] == kind]
            base = same[0] if same else {}
            e = {**base, "kind": kind, "term": term, "to": to, "none": none, "any": any_ or base.get("any") or [],
                 "status": "live",
                 "note": note, "source": base.get("source") or f"FlexSearch {rel} (search gap probes)"}
            if base and key(base.get("to") or "") != key(to):
                e.pop("id", None)
                dic.delete(base["id"], who=who, note=f"replaced by {term} -> {to}")
            try:
                dic.save(e, who=who, note=note)
                n += 1
            except ValueError as ex:
                log(f"search dictionary: release {rel} couldn't make '{term}' live ({ex})")
        # entries already in the dictionary, approved as they are (v2.44): switched live with the measurement noted
        ok = 0
        full = _approved_entries(rel)
        for term, kind, to, why in APPROVALS.get(rel, []):
            same = [e for e in dic.entries() if key(e["term"]) == key(term) and e["kind"] == kind
                    and key(e.get("to") or "") == key(to)]
            hits = [e["id"] for e in same if e["status"] != "live"]
            if not same:          # a copy without the review backlog gets the entry as it was approved
                src = full.get((key(term), kind, key(to)), {"kind": kind, "term": term, "to": to})
                e = {k: v for k, v in src.items() if k != "why"} | {
                    "status": "live", "note": f"approved (Claude for Brendan, Oct 25 2026): {why}",
                    "source": src.get("source") or f"FlexSearch {rel} (review backlog)"}
                try:
                    dic.save(e, who=who, note=e["note"])
                    ok += 1
                except ValueError as ex:
                    log(f"search dictionary: release {rel} couldn't add approved '{term}' ({ex})")
            if hits:
                try:
                    dic.set_status(hits, "live", who=who, note=f"approved (Claude for Brendan, Oct 25 2026): {why}")
                    ok += len(hits)
                except Exception as ex:  # noqa: BLE001
                    log(f"search dictionary: release {rel} couldn't approve '{term}' ({ex})")
        n += ok
        for m in _mined_entries(rel):       # v2.44: mined candidates, in review for two-person approval
            if any(key(e["term"]) == key(m["term"]) and e["kind"] == m["kind"] for e in dic.entries()):
                continue
            ev = m.get("evidence") or {}
            note = (f"{m.get('why', '')} Evidence: {ev.get('ga4_searches', 0):,} fwwebb.com searches "
                    f"({', '.join(ev.get('ga4_queries', [])[:3])}); real Webb items {ev.get('real_webb_items', 0)}; "
                    f"demo titles {ev.get('demo_titles') if ev.get('demo_titles') is not None else 'n/a'}.")
            try:
                dic.save({"kind": m["kind"], "term": m["term"], "to": m["to"], "status": "review", "note": note[:500],
                          "source": m.get("source", "mined")}, who=who, note="mined candidate")
            except ValueError as ex:
                log(f"search dictionary: release {rel} couldn't add mined '{m['term']}' ({ex})")
        ids = [e["id"] for t, kd in off for e in dic.entries() if key(e["term"]) == key(t) and e["kind"] == kd]
        if ids:
            dic.set_status(ids, "off", who=who, note="rewrote the catalog's own word")
        done.add(rel)
        applied.append(rel)
        log(f"search dictionary: release {rel} applied ({n} entries live, {len(ids)} switched off)")
    if applied:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"applied": sorted(done), "at": time.strftime("%Y-%m-%d %H:%M:%S")}))
    return applied
