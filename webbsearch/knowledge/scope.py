"""What the Knowledge Center talks about: F.W. Webb and Frank Webb products, services and the trade work around them.

Other distributors and retailers are never named in an answer, favorably, unfavorably or neutrally: a question about
one gets a Webb-only reply that doesn't repeat the name, and a FlexIQ answer that names one is replaced. Manufacturer
brands Webb sells are not competitors. The list is Webb's to keep (merchandising can extend it)."""
from __future__ import annotations

import re

COMPETITORS = [
    "Ferguson", "SupplyHouse", "Supply House", "Grainger", "Zoro", "Home Depot", "HomeDepot", "Lowe's", "Lowes", "Menard",
    "Menards", "Amazon", "Walmart", "Wal Mart", "eBay", "e-bay", "Hajoca", "Winsupply", "Winnelson", "Johnstone Supply",
    "Johnstone", "United Refrigeration", "Watsco", "Carrier Enterprise", "Gemaire", "R.E. Michel", "RE Michel",
    "Mingledorff", "Wolseley", "Hughes Supply", "HD Supply", "Fastenal", "MSC Industrial", "McMaster", "McMaster-Carr",
    "PexUniverse", "PEX Universe", "Build.com", "FaucetDirect", "Ace Hardware", "Sid Harvey", "Russell Sigler", "Habegger",
    "Plumbing Supply Co", "plumbingsupply.com", "Coburn",
]
# names that are also ordinary words ("the true value", "a central supply line"): matched only when capitalized
CASED = ["True Value", "Central Supply", "Moore Supply", "Consolidated Supply", "Granite Group", "Torrco", "Keenan Supply",
         "Core & Main", "Core and Main", "Win Supply"]
_NOT = {"amazon": r"(?!\s+(?:alexa|echo|smart|basics?\b))"}


def _alt(names, cased=False):
    out = []
    for c in sorted(names, key=len, reverse=True):
        x = re.escape(c).replace(r"\ ", r"[\s-]{0,3}").replace("'", "[’']?").replace(r"\-", r"[\s-]{0,3}")
        if cased:
            x = re.sub(r"\[\\s-\]\{0,3\}", r"[ \t-]{0,3}", x)
        out.append(x + _NOT.get(c.lower(), ""))
    return "|".join(out)


_SUFFIX = r"(?:['’]?s)?(?:\.com)?(?![\w])"
_RX = re.compile(r"(?<![\w])(?:" + _alt(COMPETITORS) + r")" + _SUFFIX, re.I)
_RXC = re.compile(r"(?<![\w])(?:" + _alt(CASED, True) + r")" + _SUFFIX)
WEBB_ONLY = ("FlexIQ answers questions about products and services from F.W. Webb and Frank Webb. Ask about an item, a "
             "model or a job, and for pricing or availability talk to your Webb branch.")
OUT_OF_SCOPE = ("FlexIQ answers questions about F.W. Webb and Frank Webb products: specifications, fit, installation, "
                "repair, error codes, warranties and your account. Try a question about a product or a job.")
TRADE = re.compile(r"\b(?:water|heater|valve|pipe|piping|fitting|pex|copper|brass|boiler|furnace|hvac|gas|vent\w*|pump|"
                   r"thermostat|anode|element|tank|tankless|code|error|flash\w*|blink\w*|install\w*|warranty|serial|model|"
                   r"part|parts|flush|drain|faucet|toilet|sink|filter|condenser|heat|cool\w*|air|btu|gpm|psi|pressure|"
                   r"leak\w*|repair|replace\w*|size|spec\w*|manual|price|stock|order|invoice|branch|webb|plumb\w*|"
                   r"submittal|diagram|wire|wiring|breaker|relief|t&p|expansion|descal\w*|scale|ignit\w*|pilot|burner|"
                   r"flame|sensor|fan|motor|compressor|refrigerant|duct|radiant|hydronic|well|softener|backflow|"
                   r"connector|connection|inlet|outlet|clearance|dimensions?|rating|input|recovery|gallon|gal|reset|button|temp\w*|hot|cold|noise|nois\w*|smell\w*|odor|rust\w*|thread\w*|solder\w*|press|crimp|sweat|npt|tube|tubing|hose|meter|regulator|furnace|boiler|ac|a/c|mini-?split|ductless|coil|line\s?set|r-?\d+\w*|faucet|shower|tub|valve|cartridge|aerator|disposal|sump|ejector|septic|sewer|trap|vent|flue|chimney|insulation|kit|job|product|item|catalog|account|invoice|quote|delivery|pickup)\b", re.I)


def competitor(text: str) -> str | None:
    m = _RX.search(text or "") or _RXC.search(text or "")
    return m.group(0) if m else None


NOT_A_QUESTION = re.compile(r"\b(?:joke|jokes|poem|poems|sonnet|haiku|limerick|riddle|song|lyrics|story|stories|essay|"
                            r"rap|fan\s?fiction|pretend\s+(?:to\s+be|you)|role[\s-]?play|ignore\s+(?:all\s+|your\s+|the\s+)?"
                            r"(?:previous\s+|prior\s+)?instructions|system\s+prompt)\b", re.I)


def not_a_question(question: str) -> bool:
    """Requests that aren't about a product or a job, whatever trade words they borrow ("a joke about water")."""
    return bool(NOT_A_QUESTION.search(question or ""))


def on_topic(question: str) -> bool:
    return bool(TRADE.search(question or "")) and not not_a_question(question)
