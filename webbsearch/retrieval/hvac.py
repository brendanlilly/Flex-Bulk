"""HVAC equipment specs, pad fit and AHRI system matchups (MatchPro, folded into search).

What is real and what is not
  * The rules are real: DOE 2023 regional minimums, the ENERGY STAR v6.1/6.2
    central AC / heat pump thresholds, the cold-climate heat pump criteria, the
    EPA AIM Act R-410A transition, and the end of the federal 25C credit. They
    follow the MatchPro code review (Sep 22, 2026); dates and thresholds are
    constants below so they are easy to audit and update.
  * The equipment data is synthetic, like the rest of the prototype catalog.
    Cabinet dimensions, EER2/HSPF2, cold-climate capacity, indoor units and AHRI
    reference numbers are generated deterministically per SKU so they are
    internally consistent (a 3-ton condenser only pairs with same-brand 3-ton
    coils and air handlers). Production swaps the generator for the AHRI
    Directory data subscription and manufacturer spec feeds (SPEC.md section 18).
"""
from __future__ import annotations

import math
import re
import zlib
from datetime import date

import numpy as np

# ---------------------------------------------------------------- rule tables
FED_MIN = {  # DOE 2023, split systems, SEER2 / EER2 / HSPF2
    ("AC", "North"): {"seer2": 13.4},
    ("AC", "Southeast"): {"seer2": 14.3},
    ("AC", "Southwest"): {"seer2": 14.3, "eer2": 11.7},
    ("HP", None): {"seer2": 14.3, "hspf2": 7.5},
}
ENERGY_STAR = {"AC": {"seer2": 15.2, "eer2": 12.0}, "HP": {"seer2": 15.2, "eer2": 11.0, "hspf2": 7.8}}
COLD_CLIMATE = {"hspf2": 8.1, "cop5": 1.75, "cap5_ratio": 0.70}
R410A_INSTALL_END = date(2026, 1, 1)   # the original EPA AIM Act install deadline for R-410A split systems
# EPA's final rule of May 26, 2026 (91 FR 31284) removed that federal deadline for systems manufactured or imported
# before Jan 1, 2025. Some states keep their own HFC rules (New York's Part 494 kept the Jan 1, 2026 deadline as of
# Dec 2025), so FlexSearch cautions rather than filters. Webb's HVAC team owns this wording.
R410A_NOTE = ("R-410A: under EPA's final rule of May 26, 2026, systems made before Jan 1, 2025 can still be installed "
              "new; state rules differ (New York has its own), so check the job's state before quoting")
CREDIT_25C_END = date(2025, 12, 31)    # IRS Form 5695: not available for property placed in service after this
PAD_MARGIN_IN = 2.0                    # common install guidance: pad extends >= 2" past the cabinet on every side

INDOOR_PREFIX = {"Rheem": "RH", "Ruud": "UH", "GE": "GE", "York": "YH", "Bosch": "BV"}
COIL_WIDTH = {1.5: 14.5, 2: 14.5, 2.5: 17.5, 3: 17.5, 3.5: 21.0, 4: 21.0, 5: 24.5}


def _rng(*key) -> np.random.Generator:
    return np.random.default_rng(zlib.crc32("|".join(map(str, key)).encode()))


def _num(s, default=None):
    m = re.search(r"[\d.]+", str(s or "").replace(",", ""))
    return float(m.group(0)) if m else default


def climate_zone(lat: float | None) -> str:
    if lat is None:
        return "5A"
    return "6A" if lat >= 44.0 else ("5A" if lat >= 41.0 else "4A")


class HVAC:
    OUTDOOR = ("ac_condenser",)

    def __init__(self, catalog, today: date):
        self.cat = catalog
        self.today = today
        self._spec: dict[int, dict] = {}
        fam = catalog.family
        self.furnace_idx = np.flatnonzero(fam == "furnace")

    # -------------------------------------------------------------- specs
    def is_outdoor(self, i: int) -> bool:
        return self.cat.family[i] in self.OUTDOOR

    def spec(self, i: int) -> dict | None:
        if not self.is_outdoor(i):
            return None
        if i in self._spec:
            return self._spec[i]
        a = self.cat.attrs(i)
        sku = str(self.cat.sku[i])
        r = _rng("spec", sku)
        ton = _num(a.get("ton"), 3.0)
        typ = a.get("type", "")
        kind = "HP" if "Heat Pump" in typ else "AC"
        cc = "Cold Climate" in typ
        inverter = cc or "Inverter" in typ
        seer2 = _num(a.get("seer"), 14.3)
        ref = a.get("ref", "R-454B")
        side = inverter and r.random() < 0.5
        if side:  # side-discharge "compact" cabinet
            w = {1.5: 32, 2: 32, 2.5: 37, 3: 37, 3.5: 40, 4: 40, 5: 42}.get(ton, 40) + (2 if cc else 0)
            d = 14 + (2 if ton >= 3.5 else 0) + int(r.integers(0, 2))
            h = {1.5: 28, 2: 28, 2.5: 33, 3: 33, 3.5: 43, 4: 43, 5: 53}.get(ton, 43)
        else:     # square, top discharge
            w = {1.5: 24, 2: 26, 2.5: 29, 3: 29, 3.5: 32, 4: 32, 5: 35}.get(ton, 30) + (3 if seer2 >= 17 else 0) \
                + int(r.choice([0, 0, 2]))
            d = w
            h = round(22 + 5 * ton + (6 if seer2 >= 17 else 0) + r.integers(-2, 3))
        eer_ratio = (0.58 + 0.08 * r.random()) if inverter else (0.66 + 0.07 * r.random())
        eer2 = round(seer2 * eer_ratio, 1)
        hspf2 = None
        cap5 = cop5 = None
        if kind == "HP":
            hspf2 = round(min(9.8, 7.5 + max(0.0, seer2 - 14.3) * 0.3 + (0.7 if cc else 0) + 0.3 * r.random()), 1)
            cap5 = round((0.72 + 0.26 * r.random()) if cc else (0.45 + 0.2 * r.random()), 2)
            cop5 = round((1.78 + 0.5 * r.random()) if cc else (1.35 + 0.3 * r.random()), 2)
        mca = round(ton * 5.6 + r.integers(0, 4) + (1 if kind == "HP" else 0))
        mocp = int(5 * math.ceil(mca * 1.6 / 5))
        s = {
            "ton": ton, "kind": kind, "cold_climate": cc, "inverter": inverter, "seer2": seer2, "eer2": eer2,
            "hspf2": hspf2, "cap5_ratio": cap5, "cop5": cop5, "refrigerant": ref,
            "cabinet": "side-discharge" if side else "top-discharge",
            "width_in": float(w), "depth_in": float(d), "height_in": float(h),
            "weight_lb": int(95 + ton * 38 + (25 if seer2 >= 17 else 0) + r.integers(-10, 11)),
            "sound_db": int(round((58 if inverter else 66) + ton * 1.2 + r.integers(-2, 3))),
            "mca": mca, "mocp": mocp, "voltage": "208/230V 1-ph",
            "clearance": ("24 in in front of the fan discharge, 12 in behind and at the ends"
                          if side else "12 in on three sides, 24 in on the service-panel side, 60 in above"),
        }
        s["pad_min"] = [s["width_in"] + 2 * PAD_MARGIN_IN, s["depth_in"] + 2 * PAD_MARGIN_IN]
        self._spec[i] = s
        return s

    def install_note(self, i: int) -> str | None:
        """Why this outdoor unit can't go in as a new install (None if it can)."""
        s = self.spec(i)
        if not s:
            return None
        mins = FED_MIN.get((s["kind"], None)) or FED_MIN.get((s["kind"], "North")) or {}
        if any(s.get(k) is not None and s[k] < v for k, v in mins.items()):
            return "Below DOE minimum for new installs"
        return None

    def attrs_extra(self, i: int) -> dict:
        """Short spec strings merged into result attrs (spec table, compare)."""
        s = self.spec(i)
        if not s:
            return {}
        out = {"cabinet": f'{s["width_in"]:g}×{s["depth_in"]:g}×{s["height_in"]:g} in',
               "discharge": "Side" if s["cabinet"] == "side-discharge" else "Top", "eer2": f'{s["eer2"]:g}'}
        if s["hspf2"]:
            out["hspf2"] = f'{s["hspf2"]:g}'
        return out

    @staticmethod
    def fits(s: dict, pad_w: float, pad_d: float | None, margin: float = PAD_MARGIN_IN) -> tuple[bool, float]:
        """Does the cabinet fit the pad with `margin` all round, in either orientation? -> (fits, spare inches)."""
        pad_d = pad_d or pad_w
        w, d = s["width_in"] + 2 * margin, s["depth_in"] + 2 * margin
        best = max(min(pad_w - w, pad_d - d), min(pad_w - d, pad_d - w))
        return best >= 0, round(best, 1)

    # ------------------------------------------------------------ checks
    def checks(self, s: dict, region: str = "North", when: date | None = None, new_install: bool = True) -> list[dict]:
        when = when or self.today
        out = []
        mins = FED_MIN.get((s["kind"], None)) or FED_MIN.get((s["kind"], region)) or {}
        fail = [f"{k.upper()} {s[k]:g} < {v:g}" for k, v in mins.items() if s.get(k) is not None and s[k] < v]
        out.append({"key": "federal", "label": "DOE federal minimum", "ok": not fail,
                    "detail": "; ".join(fail) if fail else ", ".join(f"{k.upper()} ≥ {v:g}" for k, v in mins.items())
                    + f" ({'heat pump' if s['kind'] == 'HP' else region + ' AC'})"})
        es = ENERGY_STAR[s["kind"]]
        es_fail = [f"{k.upper()} {s[k]:g} < {v:g}" for k, v in es.items() if (s.get(k) or 0) < v]
        out.append({"key": "energy_star", "label": "ENERGY STAR", "ok": not es_fail,
                    "detail": "meets v6.1 split-system criteria" if not es_fail else "; ".join(es_fail)})
        if s["kind"] == "HP":
            cc_ok = (s["hspf2"] or 0) >= COLD_CLIMATE["hspf2"] and (s["cop5"] or 0) >= COLD_CLIMATE["cop5"] and \
                    (s["cap5_ratio"] or 0) >= COLD_CLIMATE["cap5_ratio"]
            out.append({"key": "cold_climate", "label": "Cold-climate heat pump", "ok": cc_ok,
                        "detail": f"{round((s['cap5_ratio'] or 0) * 100)}% capacity and COP {s['cop5']} at 5°F, "
                                  f"HSPF2 {s['hspf2']}"})
        if s["refrigerant"] == "R-410A":
            out.append({"key": "refrigerant", "label": "Refrigerant",
                        "ok": True if (not new_install or when < R410A_INSTALL_END) else None, "detail": R410A_NOTE})
        else:
            out.append({"key": "refrigerant", "label": "Refrigerant", "ok": True,
                        "detail": f"{s['refrigerant']} (A2L): pair with an A2L-rated indoor coil with leak detection; "
                                  "check local code adoption"})
        out.append({"key": "25c", "label": "Federal 25C credit", "ok": None,
                    "detail": f"expired for equipment placed in service after {CREDIT_25C_END:%b} {CREDIT_25C_END.day}, {CREDIT_25C_END.year}"})
        return out

    # ----------------------------------------------------------- matchups
    def matchups(self, i: int, p_price=None, limit: int = 6) -> list[dict]:
        """Synthetic AHRI-certified combinations for one outdoor unit (same brand, same tonnage)."""
        s = self.spec(i)
        if not s:
            return []
        cat = self.cat
        brand = cat.brand[i]
        pre = INDOOR_PREFIX.get(brand, brand[:2].upper())
        ton = s["ton"]
        t12 = int(round(ton * 12))
        a2l = s["refrigerant"] != "R-410A"
        width = COIL_WIDTH.get(ton, 21.0)
        wl = {14.5: "A", 17.5: "B", 21.0: "C", 24.5: "D"}[width]
        r = _rng("match", cat.sku[i])
        indoor = [
            {"type": "Cased coil", "model": f"{pre}CC{t12:03d}{wl}{'L' if a2l else ''}", "role": "coil",
             "desc": f"{brand} {ton:g} ton cased evaporator coil, {width:g} in wide{', A2L w/ leak sensor' if a2l else ''}",
             "list_price": round(380 + 90 * ton + r.integers(0, 60), 2), "d_seer": 0.0 + 0.3 * r.random(), "d_hspf": 0.0},
            {"type": "Air handler, multi-speed", "model": f"{pre}AH{t12:03d}M{'L' if a2l else ''}", "role": "ah",
             "desc": f"{brand} {ton:g} ton multi-position air handler, multi-speed ECM", "list_price":
             round(980 + 180 * ton + r.integers(0, 90), 2), "d_seer": 0.3 + 0.3 * r.random(), "d_hspf": 0.1},
            {"type": "Air handler, variable-speed", "model": f"{pre}AH{t12:03d}V{'L' if a2l else ''}", "role": "ah",
             "desc": f"{brand} {ton:g} ton multi-position air handler, variable-speed ECM", "list_price":
             round(1250 + 220 * ton + r.integers(0, 120), 2), "d_seer": 0.8 + 0.7 * r.random(), "d_hspf": 0.3},
        ]
        # furnace + coil combinations: same-brand furnaces sized to the tonnage (~20-27 MBH per ton of cooling)
        lo, hi = 18_000 * ton, 30_000 * ton
        furn = [j for j in self.furnace_idx if cat.brand[j] == brand and lo <= (_num(cat.attrs(j).get("btu"), 0)) <= hi]
        furn = sorted(furn, key=lambda j: (-_num(cat.attrs(j).get("afue"), 80), str(cat.sku[j])))
        picks = []
        seen_afue = set()
        for j in furn:
            af = cat.attrs(j).get("afue")
            if af in seen_afue:
                continue
            seen_afue.add(af)
            picks.append(j)
            if len(picks) >= 2:
                break
        out = []
        for ind in indoor:
            combos = [None] if ind["role"] == "ah" else [None] + picks
            for fj in combos:
                bump = ind["d_seer"] + (0.4 if fj is not None and "Modulating" in cat.attrs(fj).get("stage", "") else 0.0)
                seer2 = round(s["seer2"] + bump, 1)
                eer2 = round(s["eer2"] + bump * 0.5, 1)
                hspf2 = round(s["hspf2"] + ind["d_hspf"], 1) if s["hspf2"] else None
                cap = int(round(ton * 12000 * (0.95 + 0.06 * r.random()), -2))
                ref_no = 200_000_000 + zlib.crc32(f"{cat.sku[i]}|{ind['model']}|{fj}".encode()) % 99_000_000
                sys = dict(s, seer2=seer2, eer2=eer2, hspf2=hspf2)
                row = {"ahri_ref": str(ref_no), "outdoor_sku": str(cat.sku[i]), "outdoor_model": cat.mfr_part[i],
                       "indoor": {k: ind[k] for k in ("type", "model", "desc", "list_price")},
                       "furnace": None, "seer2": seer2, "eer2": eer2, "hspf2": hspf2, "cooling_btuh": cap,
                       "heating_btuh_47": int(round(cap * (1.02 + 0.05 * r.random()), -2)) if s["kind"] == "HP" else None,
                       "status": "Active"}
                if fj is not None:
                    row["furnace"] = {"sku": str(cat.sku[fj]), "title": cat.title[fj], "model": cat.mfr_part[fj]}
                ck = self.checks(sys)
                row["energy_star"] = next(c["ok"] for c in ck if c["key"] == "energy_star")
                row["cold_climate"] = next((c["ok"] for c in ck if c["key"] == "cold_climate"), None)
                out.append(row)
        out.sort(key=lambda m: (-m["seer2"], m["indoor"]["list_price"]))
        return out[:limit]
