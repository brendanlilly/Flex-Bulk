"""Ask Webb's troubleshooting walk-throughs (v2.3): a symptom typed in plain words ("furnace blower runs but no heat")
gets the checks a counter pro would run through, in order, and the parts that usually fix it, each a search.

The results page already tells people "Ask Webb can walk through the diagnosis"; these are the walk-throughs, written
for contractors. They are general checks, not a diagnosis of a specific unit: model-specific steps (error codes, wiring)
come from the manufacturer's documents in the Knowledge Center. Gas, line-voltage and refrigerant steps say they are
licensed work, and the safety tiers (knowledge/safety.py) still decide what Ask Webb won't walk through."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Guide:
    id: str
    title: str
    match: str                         # regex over the lower-cased question
    checks: list[str]
    parts: list[tuple]                 # (label, catalog search[, product family the search is limited to])
    licensed: str = ""                 # the licensed-work note, when any step needs it
    kc: bool = False                   # the Knowledge Center has model documents for this
    re_: re.Pattern = field(init=False, repr=False)

    def __post_init__(self):
        self.re_ = re.compile(self.match, re.I)


GUIDES = [
    Guide("furnace_no_heat", "Furnace: blower runs, no heat",
          r"furnace.*(no heat|not heating|blowing cold|cold air)|(blower|fan) runs?.*(no|not) heat|no heat.*furnace|"
          r"furnace (won'?t|will not|doesn'?t) (light|ignite|fire)",
          ["Read the control board's status light: count the flashes and match them to the code chart on the door.",
           "Confirm the thermostat is calling for heat and the furnace switch and gas valve are on.",
           "Replace a clogged filter; a hot limit trips and shuts the burners off.",
           "Watch the sequence: inducer starts, pressure switch closes, igniter glows, burners light, flame proves. "
           "Where it stops is the part to test.",
           "Igniter: no glow, or cracked: replace it. Burners light then drop out after a few seconds: clean the flame "
           "sensor with fine abrasive pad, then check flame current.",
           "Inducer runs but the igniter never glows: check the pressure switch, its hose and the vent and "
           "condensate drain for blockage."],
          [("Hot surface igniter", "hot surface igniter", "hvac_parts"), ("Flame sensor", "flame sensor", "hvac_parts"),
           ("Pressure switch", "pressure switch", "hvac_parts"), ("Inducer motor", "inducer motor", "hvac_parts"),
           ("Control board", "control board", "hvac_parts")],
          licensed="Gas valve, burner and line-voltage work is for a licensed technician."),
    Guide("wh_pilot", "Water heater: pilot won't stay lit",
          r"pilot.*(won'?t|will not|doesn'?t|does not|keeps?).*(stay|light|lit|go(es|ing)? out)|pilot (goes|keeps going) out|"
          r"thermocouple",
          ["Hold the pilot knob down a full 60 seconds after lighting; if it stays lit only while held, the "
           "thermocouple or thermopile isn't proving.",
           "Make sure the thermocouple tip sits fully in the pilot flame; a weak, yellow or lifting flame points to a "
           "dirty pilot orifice or low gas pressure.",
           "On flammable-vapor-resistant (FVIR) models, a tripped sensor or a blocked combustion-air screen locks "
           "the pilot out: clean the screen and follow the manufacturer's reset.",
           "Replace the thermocouple (or pilot assembly) before suspecting the gas control valve."],
          [("Thermocouple", "thermocouple", "wh_parts"), ("Pilot assembly", "pilot assembly", "wh_parts"),
           ("Gas control valve", "gas control valve", "wh_parts")],
          licensed="Gas control and burner work is for a licensed technician.", kc=True),
    Guide("tankless_no_hot", "Tankless: no hot water",
          r"(no|not getting|lost|without) hot water.*tankless|tankless.*(no hot|cold water|not heating|error|code)",
          ["Read the error code on the display and look it up for that model (Knowledge Center: Error codes).",
           "Open a fixture wide: below the minimum flow rate the burner won't fire. Clean the inlet water filter "
           "screen.",
           "Scale: in hard water, flush the heat exchanger with descaling solution through the service valves.",
           "Check gas supply size and pressure with every gas appliance running; an undersized line starves a "
           "tankless.",
           "Check the vent and condensate drain for blockage."],
          [("Flow sensor", "flow sensor", "wh_parts"), ("Heat exchanger", "heat exchanger", "wh_parts")],
          licensed="Gas pressure and burner work is for a licensed technician.", kc=True),
    Guide("boiler_short", "Boiler short cycling",
          r"boiler.*(short.?cycl|cycl\w* on and off|keeps (turning|shutting) off)|short.?cycl.*boiler",
          ["Check system pressure (12-15 psi cold is typical for a residential hydronic system) and purge air "
           "from the loops.",
           "Confirm flow: circulator running, zone valves opening fully, flow checks not stuck.",
           "Look at the aquastat or boiler control differential; too narrow a differential cycles the burner.",
           "A boiler much larger than the heat loss short cycles by design: a buffer tank or outdoor reset "
           "helps.",
           "Thermostat location or a heat anticipator set wrong can cycle the call itself."],
          [("Circulator cartridge", "circulator replacement cartridge"), ("Zone valve", "zone valve"),
           ("Air separator", "air separator"), ("Expansion tank", "expansion tank"), ("Aquastat", "aquastat")],
          licensed="Burner and gas work is for a licensed technician."),
    Guide("zone_no_heat", "One zone not heating",
          r"(one|a|1|upstairs|downstairs) zone.*(no heat|not heating|cold)|zone valve.*(not|won'?t|doesn'?t)",
          ["Confirm the thermostat calls and the zone valve's end switch closes (the circulator or boiler starts).",
           "Check the zone valve actuator: no movement with 24V present means the head or motor has failed.",
           "Bleed air at the high points of that loop.",
           "Check the flow-check valve isn't stuck closed and the circulator for that zone runs."],
          [("Zone valve actuator", "zone valve actuator"), ("Zone valve", "zone valve"),
           ("Circulator cartridge", "circulator replacement cartridge"), ("Switching relay", "switching relay")]),
    Guide("ac_no_cool", "AC runs but doesn't cool",
          r"(ac|a/c|air condition\w*|heat pump|condenser).*(not cooling|warm air|blowing warm|won'?t cool|doesn'?t cool)|"
          r"(not cooling|blowing warm).*(ac|a/c|air)",
          ["Replace a dirty filter and check the indoor coil and condenser coil for dirt or ice.",
           "Outdoor unit: fan and compressor both running? A humming compressor or a fan that needs a push points "
           "to the run capacitor.",
           "Check the contactor: pitted contacts or no pull-in with 24V on the coil.",
           "Ice on the suction line means low airflow or low charge; thaw it before testing.",
           "Charge and leak checks need refrigerant gauges."],
          [("Run capacitor", "run capacitor"), ("Contactor", "contactor"), ("Condenser fan motor", "condenser fan motor"),
           ("Air filter", "pleated air filter")],
          licensed="Refrigerant work needs an EPA 608 certified technician; line-voltage work, a licensed electrician "
                   "or HVAC tech."),
    Guide("toilet_running", "Toilet keeps running",
          r"toilet.*(keeps? running|runs|won'?t stop|phantom|refill)|running toilet",
          ["Dye test: a few drops of food color in the tank; color in the bowl without flushing means the flapper "
           "leaks.",
           "Set the fill valve so the water stops about an inch below the top of the overflow tube.",
           "Check the flapper chain: a little slack, not tight, not long enough to catch.",
           "Clip the refill tube above the overflow so it doesn't siphon."],
          [("Flapper", "toilet flapper"), ("Fill valve", "toilet fill valve"), ("Tank-to-bowl kit", "tank to bowl kit")]),
    Guide("sump_no_pump", "Sump pump runs but doesn't pump",
          r"sump.*(runs?|running).*(not|doesn'?t|won'?t) (pump|empty|drain)|sump.*(won'?t|doesn'?t) (pump|turn on)",
          ["Check the discharge check valve: installed backward or stuck shut, the pump runs against it.",
           "Clear the intake screen and impeller.",
           "Look for a frozen or blocked discharge line outside.",
           "A pump that won't start: test the float switch by lifting it; replace a stuck or tangled float."],
          [("Check valve", "check valve", "gate_check_valve"), ("Sump pump", "sump pump")]),
    Guide("low_pressure", "Low water pressure, whole house",
          r"(low|weak|lost) water pressure|water pressure.*(low|dropped|weak)",
          ["Check the main shutoff and the meter valve are fully open.",
           "Read a pressure gauge on a hose bibb: a pressure-reducing valve set low or failing is the usual cause on "
           "municipal water.",
           "On a well: check the pressure switch settings and the tank's air precharge.",
           "Pressure low at one fixture only: clean the aerator or cartridge."],
          [("Pressure reducing valve", "pressure reducing valve"), ("Well pressure switch", "well pressure switch"),
           ("Well tank", "well tank")]),
    Guide("water_hammer", "Water hammer (banging pipes)",
          r"water hammer|pipes? (bang|knock|thump)|banging pipes",
          ["Note which valve causes it: quick-closing valves (washers, ice makers, solenoids) need an arrestor at "
           "the valve.",
           "Check static pressure; above 80 psi, add or reset a pressure-reducing valve.",
           "Secure loose pipe runs with straps or clamps."],
          [("Water hammer arrestor", "water hammer arrestor"), ("Pressure reducing valve", "pressure reducing valve"),
           ("Pipe clamp", "pipe clamp")]),
    Guide("tp_dripping", "T&P relief valve dripping",
          r"(t&p|t ?& ?p|temperature and pressure|relief valve).*(drip|leak|discharg|weep)",
          ["Never cap or plug a relief valve.",
           "Check for thermal expansion: a closed system (check valve or PRV at the meter) needs a thermal expansion "
           "tank sized for the heater.",
           "Check incoming pressure; over 80 psi needs a pressure-reducing valve.",
           "A valve that has discharged hot water often won't reseat: replace it."],
          [("Thermal expansion tank", "thermal expansion tank potable"), ("T&P relief valve", "t&p relief valve"),
           ("Pressure reducing valve", "pressure reducing valve")], kc=True),
    Guide("condensate", "Condensate overflow or AC leaking water",
          r"condensate.*(overflow|leak|full|backing up)|(ac|furnace|air handler).*(leak\w* water|water on the floor)",
          ["Clear the condensate line and trap (wet vac at the outlet, vinegar flush).",
           "Condensate pump: check the float moves freely and the check valve and discharge tube are clear.",
           "A tripped float safety switch stops the system: clear the cause, then reset."],
          [("Condensate pump", "condensate pump", "pumps")]),
    Guide("thermostat_blank", "Thermostat blank or no response",
          r"thermostat.*(blank|dead|no power|not working|won'?t turn on)",
          ["Replace batteries if it has them.",
           "Check for 24V between R and C: no voltage means a blown low-voltage fuse on the control board, a tripped "
           "condensate float switch or a failed transformer.",
           "A smart thermostat that reboots needs a C-wire or an adapter."],
          [("Transformer 40VA", "transformer 40va"), ("Thermostat", "programmable thermostat")],
          licensed="Line-voltage work is for a licensed electrician or HVAC technician."),
    Guide("hp_ice", "Heat pump iced up",
          r"heat pump.*(ice|iced|frozen|freez)",
          ["Some frost in heating mode is normal and clears in a defrost cycle; a solid block of ice is not.",
           "Check airflow first: dirty filter, blocked return, failed indoor blower speed.",
           "Check the defrost board and its coil sensor; a unit that never defrosts ices solid.",
           "Low refrigerant charge also ices the coil."],
          [("Defrost control", "defrost control board"), ("Air filter", "pleated air filter")],
          licensed="Refrigerant work needs an EPA 608 certified technician."),
]


def find(question: str) -> Guide | None:
    low = (question or "").lower()
    for g in GUIDES:
        if g.re_.search(low):
            return g
    return None


def answer(e, question: str, p, branch=None) -> dict | None:
    """The walk-through for a symptom, with the parts that usually fix it (each the top in-stock catalog match)."""
    g = find(question)
    if g is None:
        return None
    lines = [f"{g.title}: work through these in order."]
    lines += [f"{k}. {c}" for k, c in enumerate(g.checks, 1)]
    if g.licensed:
        lines.append(g.licensed)
    if g.kc:
        lines.append("Model-specific steps and error codes are in the manufacturer's documents: ask the Knowledge "
                     "Center with the model number.")
    cards = []
    for part in g.parts[:5]:
        label, q, fam = (tuple(part) + (None,))[:3]
        try:
            r = e.results.results(q, p, branch=branch, llm="off", filters={"family": [fam]} if fam else None)
        except Exception:  # noqa: BLE001
            continue
        if r.get("items"):
            it = r["items"][0]
            i = e.catalog.sku_to_idx.get(it["sku"])
            if i is not None:
                cards.append({"type": "product", **e.results._mini(int(i), p), "why": label})
    follow = [{"label": part[0], "q": part[1], "search": True} for part in g.parts[:4]]
    return {"answer": "\n".join(lines), "cards": cards, "kind": "troubleshoot", "guide": g.id, "engine": "rules",
            "followups": follow, "ms": 0}
