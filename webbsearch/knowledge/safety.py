"""Safety tiers for Knowledge Center questions, and the disclaimer copy (draft, for legal review).

Three signals, highest wins: the words of the question, the product it's about (a gas or line-voltage heater raises
install and repair questions to tier 2), and the error code's own tier. Tier 3 and emergencies never reach FlexIQ."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FOOTER = ("FlexIQ answers are written by AI from manufacturer documents and F.W. Webb data. They can be wrong or "
          "incomplete: check the cited pages and the manual for your exact model, and follow local codes.")
BLOCKS = {
    1: ("Caution", "Follow the manufacturer's cautions for this work; they're included in the answer."),
    2: ("Licensed professional required", "Work on gas, venting, refrigerant, line-voltage and pressure equipment must "
        "be done by a qualified, licensed professional, following the manufacturer's instructions and local codes. "
        "This answer summarizes the manufacturer's documents; it is not a substitute for them."),
    3: ("FlexIQ can't help with this", "FlexIQ can't help with changing or bypassing a safety device. A Webb expert "
        "can help you find the cause and the right part."),
    9: ("Emergency", "If you smell gas, a carbon monoxide alarm is sounding, or you see smoke, flames or a bulging tank: "
        "get everyone out of the building now. Don't use light switches or phones inside. Call 911 or your gas utility "
        "from outside."),
}
# the hazard of a library page by its section: an answer quoting it carries at least this tier
SECTION_TIER = {"Gas supply and piping": 2, "Venting": 2, "Combustion and ventilation air": 2, "Lighting and startup": 2,
                "Electrical connections": 2, "Electrical and startup": 2, "Thermostats": 2, "Status light codes": 2,
                "Error codes": 2, "Condensate": 1, "Water connections": 1, "Maintenance": 1, "Temperature setting": 1,
                "Filling and startup": 1, "Troubleshooting": 2, "Freeze protection": 1, "Safety information": 2,
                "Installing": 1, "Testing": 1, "How": 1, "Before you start": 2, "After the install": 2}
LABEL = {0: "General", 1: "Caution", 2: "High hazard", 3: "Declined", 9: "Emergency"}

_FUEL = r"(?:natural\s+gas|propane|lp|gas|fumes)"
EMERGENCY = re.compile(
    rf"\b(?:(?:smell|smelled|smelt|smelling|smells|stinks?|stinking|stunk|reeks?|reeking)\s+(?:of\s+|like\s+)?{_FUEL}\b|"
    rf"{_FUEL}\s+(?:smell|odou?r)|odou?r\s+of\s+{_FUEL}\b|"
    r"gas\s+leak(?:ing)?\b(?!\s+(?:test|check|detect\w*|solution|spray|procedure|search))|gas\s+(?:is\s+)?leaking|"
    r"leaking\s+gas|(?:hear|hiss\w*)\s+gas|"
    r"(?:co|carbon\s+monoxide)\s+(?:alarm|detector|monitor)s?\b.{0,25}\b(?:going|went|goes|sounding|sounded|beep\w*|"
    r"chirp\w*|keeps|alarm\w*|triggered|activated|is\s+on)|carbon\s+monoxide\s+(?:poisoning|leak\w*|in\s+the)|"
    r"(?:smoke|smoking)\s+(?:is\s+)?(?:from|coming|pouring)|(?:burning\s+(?:smell|odou?r)|smell\w*\s+(?:of\s+|like\s+)?(?:burning|smoke))(?!.{0,40}\b(?:first|dust|new|start\w*|season\w*)\b)|"
    r"on\s+fire|flames?\s+(?:are\s+|is\s+)?(?:coming|shooting|rolling|leaping)\s+out|flames?\b.{0,20}\b(?:coming|shooting)\s+out|"
    r"(?:tank|heater)\s+(?:is\s+)?(?:bulg\w*|swell\w*|swollen)|bulging\s+(?:tank|heater))", re.I)
# verbs that defeat a safety device outright, and verbs that only do when nothing says "to replace / test / access"
_DEFEAT = r"(?:by[\s-]?pass\w*|bybass\w*|bypas\b|jump\w*|bridg\w*|short(?:ed|ing|s)?|defeat\w*|disabl\w*|overrid\w*|" \
          r"wire\w*\s+(?:it\s+|them\s+)?(?:around|together|out)|tie\w*\s+(?:it\s+|them\s+)?(?:together|out)|" \
          r"get\s+rid\s+of|eliminat\w*|delet\w*|plug(?:s|ged|ging)?(?!\s+(?:it\s+)?in)|cap(?:s|ped|ping)?|" \
          r"put\s+a\s+(?:cap|plug)\s+on|tap(?:e|ed|ing)(?:\s+(?:down|over|shut))?|zip[\s-]?tie\w*|block\w*\s+(?:open|off))"
_SOFT = r"(?:remov\w*|take\s+out|taking\s+out|took\s+out|disconnect\w*|unplug\w*|pull\w*\s+out)"
_DEVICE = r"(?:(?:hi(?:gh)?[\s-]?)?limit(?:\s+switch)?|eco\b|e\.c\.o|energy\s+cut[\s-]?off|tco\b|thermal\s+cut[\s-]?(?:out|off)|" \
          r"cut[\s-]?(?:out|off)\b|roll[\s-]?out(?:\s+switch)?|thermal\s+(?:switch|fuse|disc)|pressure\s+switch|" \
          r"vacuum\s+switch|air\s+(?:proving\s+)?switch|door\s+(?:switch|interlock)|interlock|flame\s+(?:sensor|rod)|" \
          r"relief\s+valve|t\s*&\s*p|tpr\b|thermocouple|thermopile|lwco\b|low[\s-]water\s+cut[\s-]?off|" \
          r"fvir|flammable\s+vapou?r|safety\b|manifold\s+pressure|float\s+switch|spill\s+switch|blocked\s+vent\s+switch)"
# the devices that are only ever removed to defeat them (a thermocouple, flame sensor or T&P is removed to replace it)
_PURE = r"(?:(?:hi(?:gh)?[\s-]?)?limit(?:\s+switch)?|eco\b|tco\b|thermal\s+(?:cut[\s-]?(?:out|off)|fuse|switch|disc)|" \
        r"roll[\s-]?out(?:\s+switch)?|pressure\s+switch|vacuum\s+switch|door\s+(?:switch|interlock)|interlock\b(?!\s+panel)|" \
        r"lwco\b|low[\s-]water\s+cut[\s-]?off|fvir|flammable\s+vapou?r|spill\s+switch|safety\s+(?:switch|device|control))"
_LEGIT = re.compile(r"\b(?:replac\w*|test\w*|access\w*|inspect\w*|clean\w*|check\w*|swap\w*|new\s+one|install\w*|"
                    r"measur\w*|diagnos\w*|meter|ohm\w*|continuity|panel|cover|order)\b", re.I)
TIER3 = [re.compile(rf"\b{_DEFEAT}\b.{{0,50}}\b{_DEVICE}", re.I),
         re.compile(rf"\b{_DEVICE}.{{0,60}}\b{_DEFEAT}(?:\b|$)", re.I),
         re.compile(rf"\b(?:run|runs|running|operat\w*|work|fire|firing|start)\b.{{0,25}}\bwithout\s+(?:the\s+|a\s+|its\s+|any\s+)?"
                    rf"{_DEVICE}", re.I),
         re.compile(r"\bconvert\w*\b.{0,60}\b(?:propane|natural\s+gas|lp|ng)\b.{0,40}\b(?:without|no)\b.{0,25}\bkit", re.I),
         re.compile(r"\b(?:without|no)\b.{0,25}\bconversion\s+kit", re.I),
         re.compile(r"\b(?:run|use|burn|operat\w*|hook\w*(?:\s+up)?)\b.{0,25}\b(?:natural\s+gas|ng)\b.{0,40}\bon\s+(?:lp|propane)\b",
                    re.I),
         re.compile(r"\b(?:run|use|burn|operat\w*|hook\w*(?:\s+up)?)\b.{0,25}\b(?:propane|lp)\b.{0,40}\bon\s+(?:natural\s+gas|ng)\b",
                    re.I),
         re.compile(r"\b(?:drill|enlarg|ream|bor(?:e|ing)|open\w*\s+up|widen)\w*\s+(?:out\s+)?(?:the\s+|an?\s+)?(?:burner\s+|gas\s+)?"
                    r"(?:orifice|spud)", re.I),
         re.compile(r"\b(?:ball|shut[\s-]?off|gate|check|stop)\s+valve\b.{0,40}\b(?:t\s*&\s*p|tpr|relief|discharge)", re.I),
         re.compile(r"\b(?:t\s*&\s*p|tpr|relief\s+valve)\b.{0,80}\b(?:ball|shut[\s-]?off|gate|check|stop)\s+valve", re.I),
         re.compile(r"\badjust\w*\s+(?:the\s+)?(?:gas\s+|propane\s+|lp\s+)?(?:pressure\s+)?regulator\b(?!.{0,40}\b(?:well|pump|water|"
                    r"pressure\s+tank|air|compressor)\b)(?=.*\b(?:gas|propane|lp|meter|utility)\b)", re.I),
         re.compile(r"\b(?:vent|dump|releas|blow|let|purg)\w*\s+(?:out\s+|off\s+)?(?:the\s+|my\s+)?(?:r-?\d+\w*\s+)?refrigerant", re.I)]
_SOFT3 = [re.compile(rf"\b{_SOFT}\b.{{0,40}}\b{_PURE}", re.I), re.compile(rf"\b{_PURE}.{{0,40}}\b{_SOFT}", re.I)]
TIER2 = re.compile(r"\b(?:gas\s+(?:line|pipe|piping|supply|connection|connector|valve|pressure|leak\s+test)|connect\w*\s+"
                   r"(?:the\s+|a\s+)?gas|vent(?:ing|s|ed)?|vent\s+connector|flue|chimney|draft|combustion\s+air|pilot|"
                   r"burner|thermocouple|igniter|ignition|gas\s+control|wir(?:e|es|ing)|240\s?v|120\s?v|volts?|breaker|"
                   r"elements?|electrical|refrigerant|r-?410a|r-?32|r-?454b|braz\w*|torch|pressure\s+vessel|backflow|"
                   r"orifice|regulator|manifold\s+pressure|high\s+limit|rollout|limit\s+switch)\b", re.I)
TIER1 = re.compile(r"\b(?:t\s*&\s*p|relief\s+valve|temperature\s+and\s+pressure|expansion\s+tank|scald\w*|"
                   r"temperature\s+setting|set\s+the\s+temp\w*|anode|drain\w*|flush\w*|descal\w*|sediment|lead[\s-]?free|"
                   r"potable|solvent\s+cement|primer|hot\s+surface|solder\w*|thermostat\s+setting)\b", re.I)
# install work on a gas or line-voltage appliance is tier 2 even when no model is known
_HAZ_WORK = re.compile(r"\b(?:install\w*|replac\w*|repair\w*|hook\w*\s+up|connect\w*|convert\w*|swap\w*|wir(?:e|ing)\s+up|"
                       r"plumb\w*|set\s+up|put\s+in)\b", re.I)
_HAZ = re.compile(r"\b(?:gas|propane|lp|natural\s+gas|electric|240|208|277|480|line[\s-]voltage|tankless|furnace|boiler|"
                  r"condensing|heat\s+pump|mini[\s-]?split|air\s+condition\w*)\b", re.I)
_WORK = re.compile(r"\b(?:install\w*|replac\w*|repair\w*|fix\w*|servic\w*|connect\w*|hook\w*\s+up|mount\w*|"
                   r"start\s?up|relight\w*|light\s+the|how\s+do\s+i|how\s+to)\b", re.I)


@dataclass
class Tier:
    level: int
    reasons: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return LABEL[self.level]

    def as_dict(self) -> dict:
        b = BLOCKS.get(self.level)
        return {"level": self.level, "label": self.label, "reasons": self.reasons,
                "block": {"title": b[0], "text": b[1]} if b else None}


def classify(question: str, line=None, floor: int = 0) -> Tier:
    q = question or ""
    if EMERGENCY.search(q):
        return Tier(9, ["emergency words"])
    for rx in TIER3:
        m = rx.search(q)
        if m:
            return Tier(3, [f"asks to change a safety device: “{m.group(0)[:40]}”"])
    if not _LEGIT.search(q):
        for rx in _SOFT3:
            m = rx.search(q)
            if m:
                return Tier(3, [f"asks to remove a safety device: “{m.group(0)[:40]}”"])
    level, why = floor, (["the error code's tier"] if floor else [])
    m = TIER2.search(q)
    if m:
        level, why = max(level, 2), why + [f"“{m.group(0)}”"]
    elif TIER1.search(q):
        level, why = max(level, 1), why + [f"“{TIER1.search(q).group(0)}”"]
    if level < 2 and line is None and _HAZ_WORK.search(q) and _HAZ.search(q):
        level, why = 2, why + ["install or repair work on gas, line-voltage or refrigerant equipment"]
    if line is not None and level < 2 and _WORK.search(q) and line.cls in ("gas_tank", "tankless", "elec_tank"):
        level, why = 2, why + [f"install or repair work on a {'gas' if line.cls != 'elec_tank' else 'line-voltage'} heater"]
    return Tier(level, why)
