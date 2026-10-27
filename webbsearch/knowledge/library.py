"""The document library behind the Knowledge Center.

Real library (coming): manufacturer spec sheets, installation and operation manuals, parts lists, warranty terms,
error-code tables and serial-decoding rules, linked to Webb items through STEP. Until it arrives this module builds a
SYNTHETIC library from each copy's own catalog: every water heater series becomes a product line with four documents
(spec sheet, installation and operation manual, parts list, warranty), an error-code table, a serial rule for its
brand and a parts list mapped to the catalog's own repair parts. Every page carries the synthetic notice, and the
brand names are the demo catalog's; none of this text comes from a manufacturer.

The structures (Line, Doc, pages, code rows, parts rows) are what the real library fills in.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

NOTICE = ("Synthetic demo document: generated for the FlexSearch prototype from the demo catalog. It is not a "
          "manufacturer's document; ratings, codes and procedures are illustrative.")
REV = {"spec": "01/2026", "iom": "03/2025", "parts": "06/2025", "warranty": "01/2025", "guide": "09/2026"}
KIND_TITLE = {"spec": "Specification Sheet", "iom": "Installation & Operation Manual", "parts": "Service Parts List",
              "warranty": "Limited Warranty"}

# the parts each kind of heater lists, in callout order (callout -> catalog part type)
CLS_PARTS = {
    "gas_tank": [(1, "Gas Control Valve", "Gas Control Valve"), (2, "Thermocouple", "Thermocouple"),
                 (3, "Pilot Assembly", "Pilot Assembly"), (4, "Burner Assembly", "Burner Assembly"),
                 (5, "Anode Rod", "Anode Rod"), (6, "Dip Tube", "Dip Tube"), (7, "Drain Valve", "Drain Valve"),
                 (8, "T&P Relief Valve", "T&P")],
    "elec_tank": [(1, "Upper Thermostat", "Upper Thermostat"), (2, "Lower Thermostat", "Lower Thermostat"),
                  (3, "Upper Heating Element 4500W", "Heating Element 4500W"),
                  (4, "Lower Heating Element 4500W", "Heating Element 4500W"), (5, "Anode Rod", "Anode Rod"),
                  (6, "Dip Tube", "Dip Tube"), (7, "Drain Valve", "Drain Valve"), (8, "T&P Relief Valve", "T&P")],
    "tankless": [(1, "Control Board", "Control Board"), (2, "Igniter", "Igniter"), (3, "Flame Sensor", "Flame Sensor"),
                 (4, "Gas Control Valve", "Gas Control Valve"), (5, "Fan Motor", "Fan Motor"),
                 (6, "Flow Sensor", "Flow Sensor"), (7, "Thermal Switch", "Thermal Switch"),
                 (8, "Heat Exchanger", "Heat Exchanger")],
}
CLS_NAME = {"gas_tank": "Gas Tank Water Heaters", "elec_tank": "Electric Tank Water Heaters",
            "tankless": "Tankless Water Heaters"}

# error codes, by kind of heater: (kind, number, meaning, checks, part types, tier)
GAS_TANK_CODES = [
    ("flash", 1, "Pilot flame not detected", ["Confirm the gas supply is on at the manual shutoff valve.",
     "Relight the pilot following the lighting instructions on the heater's label.",
     "If the pilot will not stay lit, check the thermocouple connection at the gas control.",
     "Replace the thermocouple or the pilot assembly if the pilot still goes out."], ["Thermocouple", "Pilot Assembly"], 2),
    ("flash", 2, "Thermocouple output too low", ["Confirm the pilot flame surrounds the thermocouple tip.",
     "Clean the pilot assembly if the flame is weak or yellow.",
     "Replace the thermocouple if the pilot flame is correct and the code returns."], ["Thermocouple", "Pilot Assembly"], 2),
    ("flash", 3, "Temperature sensor fault", ["Turn the gas control to OFF and wait 5 minutes.",
     "Check the sensor wire connection at the gas control.",
     "If the code returns, the gas control valve must be replaced; the sensor is not serviced separately."], ["Gas Control Valve"], 2),
    ("flash", 4, "High-limit (ECO) opened: water temperature exceeded the limit", ["Do not reset the control repeatedly.",
     "Never bypass or disable the high-limit switch.",
     "Have a qualified service agency check the gas control valve and the temperature setting.",
     "The gas control valve is replaced when the high limit has opened; it is not reset in the field."], ["Gas Control Valve"], 2),
    ("flash", 5, "Flammable vapor sensor tripped", ["Ventilate the area and remove any flammable products near the heater.",
     "Do not attempt to relight the heater.", "Contact a qualified service agency: the heater must be inspected before it is returned to service."], [], 2),
    ("flash", 6, "Gas control internal failure", ["Turn the gas control to OFF.",
     "Replace the gas control valve."], ["Gas Control Valve"], 2),
    ("flash", 7, "Main burner flame lost repeatedly", ["Check that combustion air openings are clear.",
     "Check the vent for blockage and correct draft.", "Inspect and clean the burner; replace the burner assembly if damaged."],
     ["Burner Assembly"], 2),
]
TANKLESS_CODES = [
    ("E", 1, "Ignition failure", ["Confirm the gas supply is on and the inlet gas pressure is within the range on the rating plate.",
     "Purge air from a new gas line before retrying.", "Check the igniter and its wire for damage.",
     "Replace the igniter if it does not spark."], ["Igniter", "Gas Control Valve"], 2),
    ("E", 2, "Flame lost during operation", ["Check the inlet gas pressure under full fire.",
     "Check the vent termination for blockage.", "Clean or replace the flame sensor."], ["Flame Sensor"], 2),
    ("E", 3, "Overheat: the thermal switch opened", ["Check for scale in the heat exchanger and descale if needed.",
     "Clean the water inlet filter.", "Never bypass the thermal switch; replace it if it is open at room temperature."],
     ["Thermal Switch", "Heat Exchanger"], 2),
    ("E", 4, "Low water flow or flow sensor fault", ["Clean the water inlet filter.",
     "Confirm the hot water flow is above the minimum activation flow on the specification sheet.",
     "Check the flow sensor connector; replace the flow sensor if readings are erratic."], ["Flow Sensor"], 1),
    ("E", 5, "Vent or exhaust blockage", ["Inspect the vent and air intake terminations for snow, ice, nests or debris.",
     "Confirm the vent length is within the maximum in the installation manual."], [], 2),
    ("E", 6, "Combustion fan fault", ["Check the fan motor connector.", "Replace the fan motor if it does not run."],
     ["Fan Motor"], 2),
    ("E", 7, "Freeze protection active or failed", ["Confirm power is on: freeze protection needs 120 VAC.",
     "Drain the heater if power will be off in freezing weather."], [], 1),
    ("E", 9, "Inlet gas pressure low", ["Measure inlet gas pressure with all appliances on the line running.",
     "Confirm the gas line is sized for the heater's full input.", "Have the gas supplier check the regulator if pressure is low at the meter."],
     ["Gas Control Valve"], 2),
]
ELEC_TROUBLESHOOTING = [
    ("No hot water", "Check the circuit breaker. If the breaker is on, turn the power off at the breaker and press the red "
     "reset button on the upper thermostat (high limit). If it trips again, check the upper thermostat and upper element."),
    ("Not enough hot water", "The lower element or lower thermostat has usually failed. Check each element's resistance "
     "with the power off; replace an element that reads open."),
    ("Water too hot", "Check the thermostat settings. A thermostat stuck closed must be replaced; do not leave the heater "
     "running with the high limit tripping."),
    ("T&P valve drips", "Check the supply pressure and for thermal expansion in a closed system; install a thermal "
     "expansion tank where a check valve or backflow preventer is present. Replace the valve if it leaks at normal pressure."),
    ("Rumbling or popping", "Sediment on the bottom of the tank. Drain and flush the tank."),
    ("Rotten-egg odor", "A reaction between the anode and the water. Flush the tank; an aluminum-zinc anode is available."),
]


@dataclass
class Line:
    id: str
    brand: str
    series: str
    cls: str
    name: str
    skus: list = field(default_factory=list)
    models: list = field(default_factory=list)
    platform: str | None = None
    fuels: list = field(default_factory=list)
    vents: list = field(default_factory=list)
    btus: list = field(default_factory=list)
    gallons: list = field(default_factory=list)


@dataclass
class Doc:
    id: str
    kind: str
    title: str
    line_id: str | None
    rev: str
    pages: list
    synthetic: bool = True
    owner: str = ""


def _h(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def gas_input(gal: int) -> int:
    return {30: 30000, 40: 36000, 50: 40000, 55: 40000, 65: 65000, 75: 75100, 80: 75100, 100: 100000}.get(
        gal, max(30000, round(gal * 800, -3)))


def gas_recovery(btu: int) -> int:
    return round(btu * 0.8 / (8.25 * 90))


def tank_dims(gal: int, profile: str) -> tuple[int, int]:
    d = {30: 18, 40: 20, 50: 22, 55: 22, 65: 24, 75: 26, 80: 26, 100: 28}.get(gal, 22)
    h = {"Tall": 60, "Short": 48, "Lowboy": 31}.get(profile, 60) + (2 if gal >= 65 else 0)
    return h, d


def warranty_years(w: str) -> tuple[int, int, int]:
    """'10 Year' -> (tank, parts, labor) years."""
    n = int(re.search(r"\d+", w or "6").group(0))
    return n, min(n, 10), 1 if n >= 12 else 0


class Library:
    def __init__(self, catalog):
        self.cat = catalog
        self.notice = NOTICE
        self.lines: dict[str, Line] = {}
        self.docs: dict[str, Doc] = {}
        self.sku_line: dict[str, str] = {}
        self.model_line: dict[str, str] = {}
        self.codes: dict[str, list[dict]] = {}
        self._parts: dict[str, list[dict]] = {}
        self._build()

    # ------------------------------------------------------------------ lookups
    def line_for_sku(self, sku) -> Line | None:
        lid = self.sku_line.get(str(sku))
        return self.lines.get(lid) if lid else None

    def line_for_model(self, model: str) -> Line | None:
        lid = self.model_line.get(re.sub(r"[^A-Z0-9]", "", (model or "").upper()))
        return self.lines.get(lid) if lid else None

    def docs_for_line(self, line_id: str) -> list[Doc]:
        return [d for d in self.docs.values() if d.line_id == line_id]

    def docs_for_sku(self, sku) -> list[Doc]:
        l = self.line_for_sku(sku)
        return self.docs_for_line(l.id) if l else []

    def parts_rows(self, line_id: str) -> list[dict]:
        return self._parts.get(line_id, [])

    def doc_of(self, line_id: str, kind: str) -> Doc | None:
        return next((d for d in self.docs.values() if d.line_id == line_id and d.kind == kind), None)

    def sku_spec(self, sku) -> dict:
        i = self.cat.sku_to_idx.get(str(sku))
        return self.cat.attrs(i) if i is not None else {}

    def passages(self):
        for d in self.docs.values():
            for pg in d.pages:
                yield d, pg

    # ------------------------------------------------------------------ build
    def _build(self):
        cat = self.cat
        groups: dict[tuple, list[int]] = {}
        for i in range(cat.n):
            f = cat.family[i]
            if f not in ("wh_tank", "wh_tankless"):
                continue
            a = cat.attrs(i)
            if f == "wh_tank":
                cls = "elec_tank" if a.get("fuel") == "Electric" else "gas_tank"
            else:
                cls = "tankless"
            groups.setdefault((cat.brand[i], a.get("series") or "", cls), []).append(i)
        platforms = self._platforms()
        for (brand, series, cls), idx in sorted(groups.items()):
            lid = _slug(f"{brand}-{series}-{cls.replace('_tank', '')}")
            ln = Line(id=lid, brand=brand, series=series, cls=cls, name=f"{brand} {series} Series {CLS_NAME[cls]}")
            for i in idx:
                a = cat.attrs(i)
                ln.skus.append(str(cat.sku[i])); ln.models.append(str(cat.mfr_part[i]))
                for attr, bag in (("fuel", ln.fuels), ("vent", ln.vents), ("btu", ln.btus), ("gal", ln.gallons)):
                    v = a.get(attr)
                    if v not in (None, "") and v not in bag:
                        bag.append(v)
            ln.platform = self._pick_platform(platforms, brand, lid)
            self.lines[lid] = ln
            for s in ln.skus:
                self.sku_line[s] = lid
            for m in ln.models:
                self.model_line[re.sub(r"[^A-Z0-9]", "", m.upper())] = lid
            self._parts[lid] = self._parts_for(ln, platforms)
            for doc in self._docs_for(ln):
                self.docs[doc.id] = doc
            self.codes[lid] = self._codes_for(ln)
        for doc in _guides():
            self.docs[doc.id] = doc

    def _platforms(self) -> dict[str, dict]:
        """Repair-parts platforms in the catalog: series -> {brand, types: {part type: ordinal}}; plus T&P valves."""
        cat, out, tp = self.cat, {}, []
        for i in range(cat.n):
            f = cat.family[i]
            if f == "wh_parts":
                a = cat.attrs(i)
                s = a.get("series")
                if s:
                    e = out.setdefault(s, {"brand": cat.brand[i], "types": {}})
                    e["types"].setdefault(a.get("type"), i)
            elif f == "relief_valve" and "T&P" in cat.title[i] and "3/4" in cat.title[i]:
                tp.append(i)
        tp.sort(key=lambda i: (0 if "150 PSI" in cat.title[i] else 1, str(cat.sku[i])))
        self._tp = tp
        return out

    @staticmethod
    def _pick_platform(platforms: dict, brand: str, lid: str) -> str | None:
        own = sorted(s for s, e in platforms.items() if e["brand"] == brand)
        pool = own or sorted(s for s, e in platforms.items() if e["brand"] in ("Honeywell", "Robertshaw"))
        return pool[_h(lid) % len(pool)] if pool else None

    def _parts_for(self, ln: Line, platforms: dict) -> list[dict]:
        cat, rows = self.cat, []
        types = (platforms.get(ln.platform) or {}).get("types", {})
        for n, label, ptype in CLS_PARTS[ln.cls]:
            if ptype == "T&P":
                i = self._tp[_h(ln.id) % len(self._tp)] if self._tp else None
            else:
                i = types.get(ptype)
            if i is None:
                continue
            rows.append({"n": n, "part": label, "sku": str(cat.sku[i]), "mfr_part": str(cat.mfr_part[i]),
                         "brand": cat.brand[i], "title": cat.title[i]})
        return rows

    def _codes_for(self, ln: Line) -> list[dict]:
        src = {"gas_tank": GAS_TANK_CODES, "tankless": TANKLESS_CODES}.get(ln.cls, [])
        iom = self.doc_of(ln.id, "iom")
        page = next((pg["n"] for pg in iom.pages if pg["section"].startswith(("Status light", "Error codes"))), 1) if iom else 1
        by_type = {r["part"]: r for r in self._parts.get(ln.id, [])}
        out = []
        for kind, n, meaning, checks, ptypes, tier in src:
            label = f"{n} flash{'es' if n != 1 else ''}" if kind == "flash" else f"E{n:02d}"
            parts = [{"part": t, "sku": by_type[t]["sku"], "mfr_part": by_type[t]["mfr_part"]} for t in ptypes if t in by_type]
            out.append({"kind": kind, "n": n, "label": label, "meaning": meaning, "checks": checks, "parts": parts,
                        "tier": tier, "page": page, "doc_id": iom.id if iom else None})
        return out

    # ------------------------------------------------------------------ documents
    def _docs_for(self, ln: Line) -> list[Doc]:
        base = _slug(ln.id)
        mk = lambda kind, pages: Doc(id=f"{base}-{kind}", kind=kind, title=f"{ln.brand} {ln.series} Series "
                                     f"{CLS_NAME[ln.cls].rstrip('s')}s: {KIND_TITLE[kind]}", line_id=ln.id,
                                     rev=REV[kind], pages=[{"n": k + 1, "section": s, "text": t} for k, (s, t) in enumerate(pages)])
        return [mk("spec", self._spec_pages(ln)), mk("iom", self._iom_pages(ln)), mk("parts", self._parts_pages(ln)),
                mk("warranty", self._warranty_pages(ln))]

    def _models_line(self, ln: Line) -> str:
        sample = ", ".join(ln.models[:8])
        return f"Models covered: the {ln.series} series, {len(ln.models)} models, including {sample}."

    def _spec_pages(self, ln: Line) -> list[tuple[str, str]]:
        if ln.cls == "tankless":
            rows = []
            for b in sorted(ln.btus, key=lambda x: int(str(x).replace(",", ""))):
                btu = int(str(b).replace(",", ""))
                eff = 0.95 if any("Condensing" == self.sku_spec(s).get("type") for s in ln.skus[:20]) else 0.82
                g35, g70 = btu * eff / (500 * 35), btu * eff / (500 * 70)
                rows.append(f"{btu:,} BTU/h maximum input: {min(g35, 11):.1f} GPM at a 35°F rise, {g70:.1f} GPM at a 70°F rise.")
            return [("Overview", f"{ln.name}. {self._models_line(ln)} Fuel: {', '.join(ln.fuels)}. "
                     "Gas inlet connection: 3/4 in. NPT. Water connections: 3/4 in. NPT. Electrical: 120 VAC, 60 Hz. "
                     "Minimum activation flow: 0.4 GPM."),
                    ("Ratings", " ".join(rows) + " Inlet gas pressure, natural gas: 3.5 to 10.5 in. WC. Inlet gas "
                     "pressure, propane: 8.0 to 13.5 in. WC. Maximum water pressure: 150 psi.")]
        gas = ln.cls == "gas_tank"
        rows = []
        combos = sorted({(int(self.sku_spec(s).get("gal") or 50), self.sku_spec(s).get("profile") or "Tall") for s in ln.skus})
        for gal, prof in combos:
            h, d = tank_dims(gal, prof)
            if gas:
                btu = gas_input(gal)
                rows.append(f"{gal} gal {prof}: {btu:,} BTU/h input, {gas_recovery(btu)} GPH recovery at 90°F rise, "
                            f"{h} in. high, {d} in. diameter.")
            else:
                rows.append(f"{gal} gal {prof}: two 4500 W elements at 240 VAC (non-simultaneous), 20 GPH recovery at "
                            f"90°F rise, {h} in. high, {d} in. diameter.")
        conn = ("Gas inlet connection: 1/2 in. NPT. Water connections: 3/4 in. NPT. T&P relief valve: 3/4 in., 150 psi, "
                "factory installed." if gas else "Electrical: 240 VAC, single phase. Water connections: 3/4 in. NPT. "
                "T&P relief valve: 3/4 in., 150 psi, factory installed.")
        return [("Overview", f"{ln.name}. {self._models_line(ln)} Fuel: {', '.join(ln.fuels)}. {conn} "
                 f"Venting: {', '.join(v or 'Atmospheric' for v in ln.vents) if gas else 'not required'}. "
                 "Maximum working pressure: 150 psi."),
                ("Capacities and dimensions", " ".join(rows))]

    def _iom_pages(self, ln: Line) -> list[tuple[str, str]]:
        p: list[tuple[str, str]] = []
        safety = ("Read this manual before installing. Installation and service must be performed by a qualified, "
                  "licensed installer in accordance with this manual and local codes. ")
        if ln.cls == "gas_tank":
            p += [("Safety information", safety + "If you smell gas: do not light any appliance, do not touch electrical "
                   "switches, leave the building and call your gas supplier from outside. Water temperature over 125°F "
                   "can cause severe burns. Never store flammable products near the heater."),
                  ("Location and clearances", "Install indoors on a level surface near the vent and gas supply. Minimum "
                   "clearance to combustibles: 0 in. sides and back, 4 in. front, 12 in. top from the draft hood. Provide "
                   "a drain pan piped to a drain where a leak could cause damage."),
                  ("Water connections", "Connect cold water to the inlet marked COLD and hot water to the outlet marked "
                   "HOT using 3/4 in. NPT connections. Install a thermal expansion tank where a check valve, backflow "
                   "preventer or pressure-reducing valve makes the system closed. The T&P relief valve is factory "
                   "installed; pipe its discharge full size to within 6 in. of the floor or to a drain, with no valve or cap."),
                  ("Gas supply and piping", "The gas inlet is 1/2 in. NPT. Size the gas supply line for the heater's "
                   "input on the rating plate plus every other appliance on the same line, using the pipe sizing tables "
                   "in the local fuel gas code. Install a manual shutoff valve and a sediment trap ahead of the gas "
                   "control. Inlet gas pressure: 5 in. WC minimum to 14 in. WC maximum for natural gas, 11 to 14 in. WC "
                   "for propane. Test every connection for leaks with a soap solution before lighting; never use a flame."),
                  ("Venting", "Atmospheric models: use a vent connector the same size as the draft hood outlet, 3 in. "
                   "for inputs up to 40,000 BTU/h and 4 in. above. Slope the connector upward at least 1/4 in. per foot "
                   "and do not install a vent damper. Power vent models: 3 in. Schedule 40 PVC, maximum 60 equivalent "
                   "feet; each elbow counts as 5 equivalent feet. Direct vent models use the concentric vent kit "
                   "supplied with the heater."),
                  ("Combustion and ventilation air", "Provide combustion air according to the local fuel gas code. Do "
                   "not install in a room where chemicals that release corrosive vapors are stored."),
                  ("Lighting and startup", "Fill the tank completely and open a hot water faucet to purge air before "
                   "lighting. Follow the lighting instructions on the label. The status light on the gas control "
                   "glows steadily in normal operation and flashes a code when it detects a fault."),
                  ("Temperature setting", "The thermostat is set to 120°F at the factory. Higher settings increase the "
                   "risk of scald injury; a mixing valve is recommended above 120°F."),
                  ("Maintenance", "Test the T&P relief valve once a year. Drain about 2 gallons from the drain valve "
                   "every 6 months to remove sediment. Inspect the anode rod every 2 years and replace it when more than "
                   "6 in. of core wire is exposed."),
                  ("Status light codes", "The status light on the gas control flashes a code when it detects a fault. "
                   + " ".join(f"{n} flash{'es' if n > 1 else ''}: {m}." for _, n, m, *_ in GAS_TANK_CODES)
                   + " See the service parts list for replacement parts.")]
        elif ln.cls == "elec_tank":
            p += [("Safety information", safety + "Turn off power at the breaker before servicing. Never energize the "
                   "heater unless the tank is full of water: an element will burn out in seconds when dry. Water "
                   "temperature over 125°F can cause severe burns."),
                  ("Location and clearances", "Install on a level surface. Clearance to combustibles: 0 in. sides and "
                   "back; leave 24 in. in front of the element access panels for service. Provide a drain pan piped to "
                   "a drain where a leak could cause damage."),
                  ("Water connections", "Connect cold water to the inlet marked COLD and hot water to the outlet marked "
                   "HOT using 3/4 in. NPT connections. Install a thermal expansion tank where a check valve, backflow "
                   "preventer or pressure-reducing valve makes the system closed. Pipe the T&P relief valve discharge "
                   "full size to within 6 in. of the floor or to a drain, with no valve or cap."),
                  ("Electrical connections", "Supply 240 VAC single phase on a dedicated branch circuit. Size the wire "
                   "and breaker for the total wattage on the rating plate according to the National Electrical Code and "
                   "local code; 4500 W non-simultaneous models typically use a 30 A, 2-pole breaker with 10 AWG copper "
                   "wire. The heater must be grounded."),
                  ("Filling and startup", "Fill the tank completely and open a hot water faucet until water flows "
                   "steadily before turning on power."),
                  ("Thermostats", "Both thermostats are set to 120°F at the factory. The upper thermostat includes the "
                   "high-limit switch with a red reset button. Turn off power before removing the access panels."),
                  ("Maintenance", "Test the T&P relief valve once a year. Drain and flush the tank every 6 months in hard "
                   "water. Inspect the anode rod every 2 years. Replace an element that reads open on a resistance "
                   "test with the power off."),
                  ("Troubleshooting", "This heater has no fault codes. " + " ".join(f"{s}: {t}" for s, t in ELEC_TROUBLESHOOTING))]
        else:
            p += [("Safety information", safety + "If you smell gas: do not light any appliance, do not touch electrical "
                   "switches, leave the building and call your gas supplier from outside. Water temperature over 125°F "
                   "can cause severe burns."),
                  ("Location and clearances", "Indoor models mount on a wall with 12 in. clearance above and 6 in. in "
                   "front; outdoor models need 12 in. from any opening into the building. Do not install where the unit "
                   "can freeze without power."),
                  ("Venting", "Condensing models vent with 2 in. or 3 in. Schedule 40 PVC or CPVC: maximum 60 equivalent "
                   "feet with 2 in. pipe and 150 equivalent feet with 3 in. pipe; each 90° elbow counts as 5 feet. "
                   "Non-condensing models require Category III stainless steel vent. Never common-vent with another "
                   "appliance."),
                  ("Gas supply and piping", "The gas inlet is 3/4 in. NPT. Size the gas line for the heater's full "
                   "maximum input plus every other appliance on the same line using the local fuel gas code; an existing "
                   "1/2 in. line is usually too small. Inlet gas pressure: 3.5 to 10.5 in. WC natural gas, 8.0 to 13.5 "
                   "in. WC propane. Test every connection for leaks with a soap solution before operation."),
                  ("Water connections", "Install isolation valves with service ports on the hot and cold connections so "
                   "the heat exchanger can be descaled. Install a pressure relief valve rated for the heater's input."),
                  ("Condensate", "Condensing models produce acidic condensate. Pipe it to a drain through a condensate "
                   "neutralizer where local code requires one."),
                  ("Electrical and startup", "Supply 120 VAC, 60 Hz on a grounded circuit. Open a hot water faucet to "
                   "purge air from the gas line and confirm ignition."),
                  ("Maintenance", "Descale the heat exchanger once a year, or more often in hard water. Clean the water "
                   "inlet filter every 6 months."),
                  ("Error codes", "The display shows an error code when the heater detects a fault. "
                   + " ".join(f"E{n:02d}: {m}." for _, n, m, *_ in TANKLESS_CODES)
                   + " See the service parts list for replacement parts."),
                  ("Freeze protection", "The built-in freeze protection works only while the heater has power and gas. "
                   "Drain the heater if power will be off in freezing weather.")]
        p.append(("Serial number", f"The serial number is on the rating plate. {serial_rule_text(ln.brand)} Warranty "
                  "coverage is measured from the installation date; without proof of installation it is measured from "
                  "the manufacture date shown by the serial number."))
        p.append(("Service parts", f"Replacement parts use parts platform {ln.platform}. See the service parts list "
                  "for callout numbers and Webb item numbers."))
        return p

    def _parts_pages(self, ln: Line) -> list[tuple[str, str]]:
        rows = self._parts.get(ln.id, [])
        lines = " ".join(f"Callout {r['n']}: {r['part']}, manufacturer part {r['mfr_part']}, Webb item #{r['sku']}."
                         for r in rows)
        return [("Exploded view", f"{ln.name}, parts platform {ln.platform}. The exploded view shows the parts below "
                 "by callout number. Use the serial range on the rating plate to confirm the platform."),
                ("Parts list", lines or "No parts listed.")]

    def _warranty_pages(self, ln: Line) -> list[tuple[str, str]]:
        if ln.cls == "tankless":
            terms = ("Heat exchanger: 12 years residential, 5 years commercial. Parts: 5 years. Labor: 1 year "
                     "residential.")
        else:
            ys = sorted({warranty_years(self.sku_spec(s).get("warranty"))[0] for s in ln.skus})
            terms = " ".join(f"{y}-year models: tank {y} years, parts {warranty_years(f'{y} Year')[1]} years"
                             + (", labor 1 year." if warranty_years(f'{y} Year')[2] else ".") for y in ys)
        return [("Coverage", f"{ln.name}. {terms} The warranty years are part of each model's description."),
                ("Conditions", "Coverage starts on the installation date with proof of installation; without it, "
                 "coverage starts 3 months after the manufacture date shown by the serial number. Registration within "
                 "60 days is recommended. Commercial use and installations that do not follow the installation manual "
                 "are covered differently or not at all."),
                ("How to make a claim", "Claims are made through the distributor that sold the heater. Bring the model "
                 "and serial number, proof of installation and the failed part.")]


SERIAL_RULES = {
    # brand: (prefix, pattern, how the date is encoded, a human description)
    "Rheem": ("RH", r"^RH(\d{2})(\d{2})(\d{6})$", "yy_ww", "Rheem serials start with RH, then the 2-digit year and the 2-digit week (RH2114… is week 14 of 2021)."),
    "GE": ("GE", r"^GE([A-HJ-M])(\d{2})(\d{6})$", "m_yy", "GE serials start with GE, then a month letter (A = January … M = December, I skipped) and the 2-digit year (GED21… is April 2021)."),
    "Bradford White": ("BW", r"^BW(\d{2})(\d{2})(\d{5})$", "yy_mm", "Bradford White serials start with BW, then the 2-digit year and the 2-digit month (BW2104… is April 2021)."),
    "American Standard": ("AS", r"^AS(\d{2})(\d{2})(\d{6})$", "ww_yy", "American Standard serials start with AS, then the 2-digit week and the 2-digit year (AS1421… is week 14 of 2021)."),
    "Bosch": ("BO", r"^BO(\d{2})(\d{2})-(\d{5})$", "yy_mm", "Bosch serials start with BO, then the 2-digit year and month, a dash and 5 digits (BO2104-… is April 2021)."),
    "Rinnai": ("RI", r"^RI(\d{2})(\d{2})(\d{6})$", "yy_mm", "Rinnai serials start with RI, then the 2-digit year and month (RI2104… is April 2021)."),
    "Navien": ("NV", r"^NV(\d{4})(\d{2})(\d{5})$", "yyyy_mm", "Navien serials start with NV, then the 4-digit year and 2-digit month (NV202104… is April 2021)."),
    "Noritz": ("NZ", r"^NZ(\d{2})(\d{2})(\d{6})$", "yy_ww", "Noritz serials start with NZ, then the 2-digit year and week (NZ2114… is week 14 of 2021)."),
}


def serial_rule_text(brand: str) -> str:
    r = SERIAL_RULES.get(brand)
    return r[3] if r else "This brand's serial format is not in the library."


def _guides() -> list[Doc]:
    g = [
        ("guide-gas-wh-replacement", "Webb Guide: Gas water heater replacement checklist", [
            ("Before you start", "Shut off the gas at the appliance shutoff valve, turn the control to OFF, close the "
             "cold water supply and drain the old heater. Inspect the vent and chimney before reconnecting: a damaged "
             "or undersized vent must be corrected. Gas work must be done by a licensed installer."),
            ("Parts commonly needed", "Gas appliance connector, sediment trap fittings, manual gas shutoff valve, water "
             "heater connectors or dielectric unions, T&P discharge pipe, drain pan, thermal expansion tank where the "
             "system is closed, and seismic straps where required."),
            ("After the install", "Leak-test every gas joint with soap solution, fill the tank and purge air before "
             "lighting, check the draft at the draft hood after 5 minutes of operation, set the temperature to 120°F and "
             "register the warranty.")]),
        ("guide-expansion-tanks", "Webb Guide: Thermal expansion tanks", [
            ("When one is needed", "A check valve, backflow preventer or pressure-reducing valve on the water service "
             "makes the system closed: heated water has nowhere to expand and the T&P relief valve drips. A thermal "
             "expansion tank on the cold supply absorbs that expansion."),
            ("Sizing", "Size the tank from the manufacturer's chart using the water heater volume, the supply pressure "
             "and the temperature setting. Set the tank's air precharge to the incoming water pressure before installing.")]),
        ("guide-tp-valves", "Webb Guide: T&P relief valves", [
            ("Installing", "Use a 3/4 in. T&P relief valve rated at or above the heater's input. Pipe the discharge full "
             "size, downhill, to within 6 in. of the floor or to an approved drain. Never install a valve, plug or cap "
             "on the discharge line."),
            ("Testing", "Lift the test lever once a year and let it snap back; water should flow freely and stop. A valve "
             "that keeps dripping at normal pressure must be replaced.")]),
        ("guide-tankless-descaling", "Webb Guide: Descaling a tankless water heater", [
            ("Why", "Scale in the heat exchanger reduces flow and causes overheat codes. Descale once a year, or more "
             "often in hard water."),
            ("How", "Turn off power and gas, close the isolation valves and connect a small pump and hoses to the "
             "service ports. Circulate a descaling solution or white vinegar for 45 to 60 minutes, flush with fresh "
             "water for 5 minutes, clean the inlet filter and restore service.")]),
    ]
    return [Doc(id=i, kind="guide", title=t, line_id=None, rev=REV["guide"], owner="Webb Plumbing",
                pages=[{"n": k + 1, "section": s, "text": x} for k, (s, x) in enumerate(pp)]) for i, t, pp in g]
