"""Guardrails on what customers ask and what FlexIQ and Webb's rules say back (v2.2).

Two halves:

  inspect(question)   reads the question: a set reply for some (pricing talk, a complaint about Webb, an injury or
                      damage claim, prompt injection, abuse, "where can I buy"), and flags for review on others
                      (warranty, repair, lifecycle, code, recall, profanity, personal data).
  violations(text)    reads an answer: profanity; anything negative about F.W. Webb or its people; comments on price;
  scrub(text)         a place to buy or find other than F.W. Webb online or a Webb branch; and claims a source doesn't
                      make (a lifespan, a guarantee, "most reliable", a price, an item or model number, a quantity).
                      violations() lists what's wrong (a FlexIQ answer with any is replaced); scrub() drops the
                      sentences that have them (Ask Webb's answers).

redact() takes personal data (card, SSN, e-mail, phone) out of anything logged, forwarded or sent to FlexIQ.

Patterns, not a model: they're fast, testable and explainable to a reviewer. They over-flag on purpose; the review
log shows what tripped and why."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ copy (drafts, for legal and marketing review)
PRICING_REPLY = ("Your account price is shown on each item and in your cart. For quotes, job or volume pricing, talk to "
                 "your F.W. Webb branch.")
COMPLAINT_REPLY = ("I'm sorry about that. Your F.W. Webb branch manager wants to hear it and can make it right; "
                   "I've noted it for the team.")
LEGAL_REPLY = ("I'm sorry to hear that. If anyone is hurt, call 911. For an injury, property damage or a claim, contact "
               "your F.W. Webb branch manager directly so the right people can help.")
INJECTION_REPLY = ("FlexIQ answers questions about F.W. Webb and Frank Webb products: specifications, fit, installation, "
                   "repair, error codes, warranties and your account. Try a question about a product or a job.")
ABUSE_REPLY = "I'm here to help with products and jobs. What can I find for you?"
WHERE_REPLY = ("F.W. Webb carries it: order on fwwebb.com or pick it up at {branch}. Your branch can also check stock "
               "at nearby Webb branches and order what isn't on the shelf.")
NOTES = {
    "warranty": "Warranty terms are the manufacturer's and depend on the model, registration and installation; the "
                "warranty document governs. Your Webb branch can confirm coverage and handle the claim.",
    "lifecycle": "Service life depends on water quality, installation, use and maintenance. FlexIQ gives only what the "
                 "manufacturer's documents state; it doesn't predict how long a unit will last.",
    "code": "Code requirements vary by state and town; your local code official decides what's required.",
    "recall": "Your Webb branch can check whether a model and serial number are covered by a recall.",
    "repair": "",
}
# questions with nothing to do with F.W. Webb, answered lightly and steered back (draft copy, for marketing review).
# Two per kind, picked by the question so the same question gets the same reply.
OFF_TOPIC = {
    "weather": ("I don't do forecasts, only the kind of heat you can install. Ask me about a boiler, a furnace or a water "
                "heater from F.W. Webb and I'm all yours.",
                "No forecast from me, but whatever it's doing outside, F.W. Webb has the heating and cooling for it. What "
                "are you working on?"),
    "clothes": ("Knots aren't my specialty unless they're on a pipe hanger. For fittings, valves and the rest of F.W. "
                "Webb's plumbing aisle, I'm your assistant.",
                "I'll leave the shoelaces to you. If it's plumbing, heating or cooling, F.W. Webb has you covered."),
    "relationships": ("That one's above my pay grade. I only handle relationships between pipes and fittings. What can I "
                      "find for you at F.W. Webb?",
                      "I'm no help with that, but I can tell you which F.W. Webb fitting goes with which pipe. What are "
                      "you working on?"),
    "food": ("I can't help with the recipe, but I can help you pick an F.W. Webb water heater that keeps up with the dishes.",
             "No recipes here. For the kitchen faucet, the disposal or the gas line to the range, F.W. Webb has it."),
    "sports": ("I'll leave the scores to the experts. A sump pump from F.W. Webb before the next big rain, that I can do.",
               "No scores from me. F.W. Webb's lineup is plumbing, heating, cooling and the parts to keep them running."),
    "politics": ("I stay neutral on everything except pipe sizing. What can I find for you at F.W. Webb?",
                 "That's not my department. F.W. Webb's valves, boilers and water heaters are. What are you working on?"),
    "money": ("I can't pick stocks, but I can pick the right valve from F.W. Webb. What does the job need?",
              "No investment tips here. For plumbing, heating and cooling products, F.W. Webb is the place."),
    "trivia": ("That's outside my aisle. I'm FlexIQ, F.W. Webb's assistant for products, orders and jobs. What are you "
               "working on?",
               "Good question, wrong counter. Ask me about an F.W. Webb product, an order or a job and I'll get you an answer."),
    "about_me": ("I'm FlexIQ, F.W. Webb's assistant: no feelings, lots of fittings. What can I find for you?",
                 "Just an assistant who knows the F.W. Webb catalog. Ask me about a product, an order or a job."),
    "creative": ("I'd tell you a plumbing joke, but it might leak. For products, orders and jobs at F.W. Webb, ask away.",
                 "My poetry is mostly part numbers. For plumbing, heating and cooling at F.W. Webb, I'm all yours."),
    "entertainment": ("I don't stream shows, but F.W. Webb can stream hot water: ask me about a tankless "
                      "heater. What are you working on?",
                      "No reviews from me. For plumbing, heating and cooling at F.W. Webb, I'm the one to ask."),
    "travel": ("I can't book the trip, but I can make sure the house has heat and hot water when you get back. What do "
               "you need from F.W. Webb?",
               "No travel tips here. Plumbing, heating and cooling from F.W. Webb, that's my territory."),
    "health": ("For that, please talk to a doctor. For anything plumbing, heating or cooling, F.W. Webb can help.",
               "That's one for a doctor. For products, orders and jobs at F.W. Webb, I'm here."),
    "general": ("That's outside my aisle. I'm FlexIQ, F.W. Webb's assistant for products, orders and jobs. What are you "
                "working on?",
                "I only know plumbing, heating, cooling and your F.W. Webb account. Ask me about a product or a job."),
}
SAFETY_REPLY = ("I'm only able to help with F.W. Webb products and services, but please don't go through this alone. If "
                "you're thinking about hurting yourself, call or text 988 (the Suicide & Crisis Lifeline). If you're in "
                "danger, call 911.")
SET_REPLIES = {"pricing": PRICING_REPLY, "complaint": COMPLAINT_REPLY, "legal": LEGAL_REPLY,
               "injection": INJECTION_REPLY, "abusive": ABUSE_REPLY}

# ------------------------------------------------------------------ patterns
# profanity, including spaced, starred and leet spellings ("f u c k", "sh!t", "a$$hole", "f*ck", "fck")
_G = r"[\W_]*"
_PROFANE = re.compile(
    rf"(?<![a-z])(?:f+{_G}(?:u+|v|\*+|@)?{_G}(?:c+|\*+)?{_G}k+(?:ing|ed|er|ers|s|in)?|f+u+q+|"
    rf"s+{_G}h+{_G}(?:i+|1|!+|\*+|y){_G}t+(?:s|ty|ting|head)?|bull{_G}sh\w*|horse{_G}sh\w*|"
    rf"b+{_G}(?:i+|1|!+|\*+){_G}t*{_G}c+{_G}h+\w*|bastard\w*|(?:a|@){_G}(?:s|\$){_G}(?:s|\$){_G}h+{_G}(?:o|0|\*)+{_G}l+{_G}e+s?|"
    rf"jack{_G}ass|dumb{_G}ass|dick(?:head)?s?|piss(?:ed)?|c+{_G}(?:u+|\*+){_G}n+{_G}t+s?|mother{_G}f\w+|goddam\w*|"
    rf"damn(?:ed|it)?|wtf|crap(?:py)?|prick|twat|wank\w*)(?![a-z])", re.I)
_WEBB = (r"(?:f\.?\s?w\.?\s?webb|frank\s+webb|webb(?:['’]s)?|(?:the|your|my|our|this|that|local)\s+branch(?:es)?|"
         r"branch\s+(?:staff|people|manager|employees?)|counter\s+(?:staff|people|guys?|help)|(?:guys?|people|folks|staff)\s+"
         r"(?:at|behind)\s+the\s+(?:counter|branch|desk)|(?:the|your|their|webb['’]?s?)"
         r"\s+(?:staff|employees?|people|team|reps?|salespeople|sales\s*(?:reps?|people|team)|associates?|"
         r"customer\s+service|inside\s+sales|drivers?))")
# insults and accusations: wrong wherever they are in a sentence about Webb or its people
_NEG_STRONG = (r"(?:rude|incompetent|unhelpful|useless|lazy|clueless|inept|negligent|careless|stupid|idiots?|jerks?|"
               r"dishonest|liars?|lied|lying|untrustworthy|can['’]?t\s+be\s+trusted|scam\w*|rip(?:[\s-]?off|ped\s+(?:me\s+)?off)|"
               r"overcharg\w*|dropped\s+the\s+ball|let\s+(?:me|us|you|them)\s+down|screw(?:ed|s)?\s+up|messed\s+up|sucks?|"
               r"a\s+joke|garbage|trash|terrible|awful|horrible|disappoint\w*|worst)")
# plain words ("bad", "slow", "wrong", "error") only when they're said of Webb or its people, a few words on
_NEG_WEAK = r"(?:bad|poor(?:ly)?|slow|unreliable|worse|mistakes?|wrong|errors?|at\s+fault|to\s+blame)"
_NEG = rf"(?:{_NEG_STRONG}|{_NEG_WEAK})"
_NEG_WEBB_IN = re.compile(rf"\b{_WEBB}\s+(?:[\w'’,]+\s+){{0,6}}?{_NEG_STRONG}\b|\b{_WEBB}\s+(?:[\w'’]+\s+){{0,3}}?{_NEG_WEAK}\b|"
                          rf"\b{_NEG}\s+(?:service|experience|support|help|treatment)\s+(?:from|at)\s+(?:the\s+)?{_WEBB}", re.I)
_NEG_WEBB_OUT = re.compile(rf"\b{_WEBB}\b(?=.*\b{_NEG_STRONG}\b)|\b{_NEG_STRONG}\b(?=.*\b{_WEBB}\b)|"
                           rf"\b{_WEBB}\s+(?:[\w'’]+\s+){{0,3}}?{_NEG_WEAK}\b|"
                           rf"\b{_NEG}\s+(?:service|experience|support|help|treatment)\s+(?:from|at)\s+(?:the\s+)?{_WEBB}", re.I)

_PRICE_TALK_IN = re.compile(
    r"\b(?:good|fair|great|best|better|lower|right)\s+(?:price|deal)|\bdeal\s+on\b|discount\w*|price[\s-]?match\w*|"
    r"beat\s+(?:the|their|a|that|any)\s+price|negotiat\w*|overpriced|too\s+(?:expensive|pricey)|"
    r"(?:costs?|charg\w*|pay\w*)\s+too\s+much|too\s+much\s+money|"
    r"(?:why|how)\s+(?:is|are)\s+(?:it|this|these|they|that)\s+(?:so\s+)?(?:expensive|pricey)|so\s+expensive|"
    r"prices?\s+(?:going|go|will\s+go|to\s+go)\s+(?:up|down)|price\s+(?:increase|hike|drop|cut)s?|"
    r"(?:will|do|does|are)\s+prices?\s+(?:go|rise|increase|drop|fall|change)|when\s+(?:will|do)\s+prices|on\s+sale|"
    r"coupons?|promo(?:tion(?:al)?)?\s+codes?|markup|your\s+margin|worth\s+the\s+(?:money|price)|"
    r"value\s+for\s+(?:the\s+)?money|cash\s+discount|contractor\s+pricing|rebates?", re.I)
# what a product costs (fine for Ask Webb, which shows the account's price; the Knowledge Center sends it to the page)
_PRICE_LOOKUP = re.compile(
    r"\b(?:price|prices|pricing|priced)\b|"
    r"\bhow\s+much\s+(?:is|are|for)\s+(?!.{0,40}\b(?:clearance|weigh\w*|pressure|flow|capacity|difference|space|room|"
    r"btu|gpm|amps?|draw|gas\s+(?:does|do|is))\b)|"
    r"\bhow\s+much\s+(?:does|do|would|will)\s+(?:it|this|that|they|these|one|a|an|the)\b[^?]{0,40}\bcost\b(?!\s+to\s+(?:run|operate))|"
    r"\bwhat\s+(?:does|do|would|will)\s+(?:it|this|that|they|these|one|a|an|the)\b[^?]{0,40}\bcost\b(?!\s+to\s+(?:run|operate))|"
    r"(?<!operating\s)(?<!running\s)(?<!energy\s)(?<!utility\s)(?<!fuel\s)\bcost\s+of\s+(?:a|an|the|this|these|one)\b(?![^?]{0,30}\b(?:operat\w*|running|energy|ownership))", re.I)
_PRICE_OUT = re.compile(
    r"\b(?:cheap(?:er|est|ly)?|inexpensive|expensive|pricey|overpriced|affordabl[ey]|bargains?|a\s+steal|"
    r"(?:good|great|best|better|fair)\s+(?:deal|price|value|buy)|(?:low|lower|lowest|higher|better)\s+prices?|"
    r"(?:low|lower|lowest|higher|high)[\s-]cost|pay\s+(?:less|more)|"
    r"discount\w*|on\s+sale|sale\s+price|price\s+(?:increase|hike|drop|cut|match)\w*|price\s+goes\s+(?:up|down)|"
    r"prices?\s+(?:will|may|might|could|are\s+going\s+to|tend\s+to|usually|often|typically|generally)\s+(?:go|rise|increase|"
    r"drop|fall|climb)\w*|rebates?|"
    r"save\s+(?:you\s+)?money|savings|worth\s+the\s+(?:money|price|cost)|value\s+for\s+(?:the\s+)?money|markup|"
    r"margins?|coupons?|promo(?:tion)?\s+codes?|costs?\s+(?:less|more)|(?:less|more)\s+expensive|budget[\s-]friendly|"
    r"premium\s+price|money[\s-]saving|first\s+cost)\b", re.I)
_AMOUNT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")

_WHERE_IN = re.compile(
    r"\bwhere\s+(?:can|do|could|should|would|might)\s+(?:i|we|you|one)\s+(?:buy|order|purchase|pick\s+up|source|"
    r"get\s+hold\s+of|get|find)\s+(?:a|an|one|some|it|this|these|them|that|those|the\s+(?:best|right|same)|replacement|"
    r"new|parts?|[a-z]+\s+(?:water\s+heaters?|heaters?|tanks?|boilers?|furnaces?|valves?|parts?))\b|"
    r"\bwhere\s+to\s+(?:buy|order|purchase)|\bwho\s+(?:sells|carries|stocks)\b|\bwho\s+has\s+(?:it|this|one|them|these)\s+"
    r"in\s+stock|\b(?:buy|order|purchase)\s+(?:it|this|one|them|these)?\s*(?:online|locally)\b|\bnear\s+me\b|"
    r"\bnearest\s+(?:store|location|branch|supplier|supply)|\bstore\s+hours\b|\bis\s+there\s+a\s+(?:store|branch|location)",
    re.I)
# "where can I find the serial number / the reset button / the manual" is about the unit, not where to buy it
_WHERE_NOT = re.compile(r"\b(?:serial|model\s+(?:number|no)|rating\s+plate|nameplate|data\s+plate|reset|button|switch|"
                        r"manual|instructions|diagram|warranty|code|label|sticker|valve\s+on|drain\s+valve|anode|"
                        r"located|location\s+of|on\s+(?:the|my|a)\s+(?:unit|heater|tank|furnace|boiler))\b", re.I)
_ELSEWHERE = re.compile(
    r"\b(?:hardware\s+stores?|home[\s-]improvement\s+(?:stores?|centers?|retailers?)|home\s+centers?|big[\s-]box|"
    r"supply\s+houses?|plumbing\s+suppl(?:y|ies)\s+(?:stores?|houses?|shops?)|online\s+(?:retailers?|stores?|"
    r"marketplaces?|sellers?|vendors?|shops?)|"
    r"(?:other|another|any|local|nearby|a\s+different|different)\s+(?:[a-z]+\s+)?(?:distributors?|suppliers?|retailers?|"
    r"stores?|vendors?|sellers?|shops?|wholesalers?|dealers?|supply\s+houses?)|"
    r"(?:the\s+)?manufacturer['’]?s?\s+(?:web\s*site|site|online\s+store|store)|craigslist|facebook\s+marketplace|offerup|"
    r"etsy|alibaba|aliexpress|temu|wish\.com|"
    r"(?:available|sold)\s+(?:at|from|through)\s+(?:most|many|any|all|other|major))\b", re.I)
_URL = re.compile(r"\b(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+(?:com|net|org|io|co|us|gov|edu|biz|info|store|shop|ca|uk|"
                  r"de|au|mx|me|tv|ly|app|ai|online|site|xyz))\b(?:/[^\s)]*)?", re.I)
WEBB_DOMAINS = ("fwwebb.com", "frankwebb.com")

_CLAIM = re.compile(
    r"\b(?:guarantee[ds]?|guaranteeing|will\s+never\s+(?:fail|break|leak|rust|wear)|never\s+(?:fails|breaks|leaks)|"
    r"100\s?%|risk[\s-]free|no\s+risk|always\s+works|lifetime\s+(?:warranty|guarantee)|promise[sd]?|"
    r"(?:most|more|very|highly|extremely|super)\s+(?:reliable|durable|dependable|popular)|longer[\s-]lasting|built\s+to\s+last|"
    r"best\s+(?:brand|quality|choice|product|on\s+the\s+market|in\s+the\s+industry)|top[\s-]rated|industry[\s-]leading|"
    r"market[\s-]leading|leading\s+(?:brand|manufacturer)|best[\s-]selling|quietest|(?:on|in)\s+the\s+market|"
    r"#\s?1\b|number\s+one|maintenance[\s-]free|never\s+needs?\s+(?:maintenance|service)|outlives?|outlast\w*|"
    r"years\s+of\s+(?:service|life|use)|"
    r"(?:last|lasts|lasted|lasting)\s+(?:for\s+)?(?:about\s+|around\s+|up\s+to\s+|over\s+|at\s+least\s+)?(?:\d+(?![\d-])|a\s+decade|decades|"
    r"many|years|a\s+long|a\s+lifetime)|life\s*(?:span|time|expectancy)\s+(?:of|is)|service\s+life\s+(?:of|is)|"
    r"(?:expected|should|will|typically|usually|generally|often|normally)\s+(?:to\s+)?last\w*)\b", re.I)

_WARRANTY = re.compile(r"\b(?:warrant\w*|guarantee\w*|covered\s+(?:by|under)\s+(?:the\s+|a\s+|its\s+)?(?:warranty|guarantee)|"
                       r"(?:is|are|it['’]?s|still)\s+(?:it\s+|this\s+|that\s+)?covered\b(?!\s+by\s+(?:the|an?)\s+(?!warranty))|"
                       r"coverage|register\s+(?:my|the|this|it|a|our)\s+(?:heater|unit|product|warranty|tank|boiler|furnace|it)|"
                       r"register\s+it\b|registration|warranty\s+claim|rma|prorat\w*)\b", re.I)
_REPAIR = re.compile(r"\b(?:repair\w*|fix(?:es|ed|ing)?\b|broken|not\s+working|stopped\s+working|won['’]?t\s+(?:start|light|heat|"
                     r"turn\s+on|work|stay\s+lit|ignite|fire)|doesn['’]?t\s+(?:work|heat|start|light)|isn['’]?t\s+working|"
                     r"leak(?:s|ing|ed|y)?|drip(?:s|ping|py)?|troubleshoot\w*|diagnos\w*|no\s+(?:hot\s+water|heat)|tripp\w*|"
                     r"keeps\s+(?:going\s+out|tripping|shutting|cycling)|error\s+codes?|flash(?:es|ing)|blink\w*|noisy|"
                     r"rumbl\w*|popping|fail(?:ed|ing|s|ure)?)\b", re.I)
_LIFECYCLE = re.compile(
    r"\b(?:how\s+long\s+(?:will|does|do|should|can|would|is)\b.{0,40}\b(?:last|run|go|good\s+for)|"
    r"life\s*(?:span|time|expectancy|cycle)|service\s+life|useful\s+life|end\s+of\s+(?:its\s+)?life|"
    r"years?\s+(?:of\s+(?:life|service))|how\s+old\s+is|\d+\s+years?\s+old|when\s+(?:should|do|to)\s+(?:i|we|you)?\s*"
    r"replace|due\s+for\s+(?:a\s+)?replacement|should\s+(?:i|we)\s+replace|replace\s+or\s+repair|repair\s+or\s+replace|"
    r"wear\s+out|durab\w*|reliab\w*|last\s+longer)", re.I)
_CODE = re.compile(r"\b(?:up\s+to\s+code|to\s+code|code[\s-]compliant|meets?\s+(?:the\s+)?code|code\s+(?:requires?|"
                   r"allows?|requirements?|says)|building\s+code|plumbing\s+code|fuel\s+gas\s+code|(?:ipc|upc|imc|ifgc|"
                   r"irc|nec)\b|nfpa\s*\d+|inspector|permits?|(?<!error\s)(?<!fault\s)(?<!status\s)(?<!flash\s)"
                   r"(?<!the\s)code\s+(?:for|on|about)\s+(?:the|a|an)\b|what\s+(?:is|does)\s+the\s+code\s+(?:for|on|say))", re.I)
_RECALL = re.compile(r"\b(?:recalled|recall\s+(?:on|for|notice|list|number|campaign)|(?:a|any|the|is\s+there\s+a|been\s+a)\s+"
                     r"recall\b|recalls\b)", re.I)
_LEGAL = re.compile(
    r"\b(?:lawsuits?|sue|suing|attorneys?|lawyers?|(?:is|are|be|held|hold\s+\w+|am|was|were)\s+liable(?!\s+to\b)|liability|negligen\w*|"
    r"injur(?:y|ies|ed)|(?:got|was|were|been|is|are)\s+(?:hurt|burned|burnt|scalded)|"
    r"burn(?:ed|t)\s+(?:my|his|her|their|our)\s+(?:son|daughter|kid|child|hand|arm|leg|face|skin|wife|husband|customer)|"
    r"scalded|hospital\w*|insurance\s+(?:claim|company|adjuster)|who\s+(?:pays|is\s+paying|will\s+pay|is\s+responsible)\b|"
    r"pay\s+for\s+the\s+damage|compensat\w*|small\s+claims|reimburs\w*)\b", re.I)
_DAMAGE = re.compile(r"\b(?:property\s+damage|water\s+damage|flood(?:ed|ing)?|damaged\s+(?:my|the|our)\s+(?:floor|ceiling|"
                     r"basement|house|home|ceiling|carpet))\b", re.I)
_INJECTION = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+|previous\s+|prior\s+|above\s+|earlier\s+)*"
    r"(?:instructions|rules|prompts?|guidelines|directions)|system\s+prompt|(?:reveal|show|print|repeat)\s+(?:me\s+)?"
    r"(?:your|the)\s+(?:prompt|instructions|rules)|you\s+are\s+now\b|act\s+as\s+(?:if\s+you|an?\s+(?:ai|assistant|chatbot|"
    r"different|unrestricted|unfiltered|evil|jailbroken))\b|pretend\s+(?:to\s+be|you(?:['’]re|\s+are))|"
    r"jailbreak|developer\s+mode|\bdan\s+mode|do\s+anything\s+now", re.I)
_AT_YOU = re.compile(r"\b(?:you|you['’]re|your|ur|u|this\s+(?:bot|thing|ai)|flexiq|webb)\b", re.I)

# off topic: what it's about, unless the question is plainly about a product, an order or a job
_OFF = [
    ("weather", r"\bweather\b(?!\s*(?:strip\w*|proof\w*|seal\w*|resistant|tight|head|cap|hood))|\bforecast\b|"
                r"\bgoing\s+to\s+(?:rain|snow)\b|\btemperature\s+(?:in|outside|today|tomorrow)\b"),
    ("clothes", r"\btie\s+(?:my|a|your|his|her)?\s*(?:shoes?|shoelaces?|necktie)\b|\bshoelaces?\b|\bwhat\s+(?:should\s+i\s+)?"
                r"(?:to\s+)?wear\b|\boutfit\b|\bfashion\b"),
    ("relationships", r"\bex[\s-]?(?:wife|husband|girlfriend|boyfriend|partner)\b|\bgirlfriend\b|\bboyfriend\b|\bdating\b|"
                      r"\bbreak(?:ing)?\s+up\b|\bbreakup\b|\bdivorce\b|\b(?:marriage|relationship|dating)\s+advice\b|"
                      r"\bmother[\s-]in[\s-]law\b|\bmy\s+(?:wife|husband|spouse|kids?|teenager)\s+(?:is|keeps|won['’]?t|"
                      r"texting|texts|said|hates)\b"),
    ("food", r"\brecipes?\b|\bhow\s+(?:do\s+i|to|long\s+to)\s+(?:cook|bake|grill|roast|boil\s+(?:an?\s+)?eggs?|make\s+"
             r"(?:pasta|pizza|dinner|cookies|bread|lasagna|coffee))\b|\bwhat(?:['’]s|\s+is)\s+for\s+dinner\b|\brestaurants?\b"),
    ("sports", r"\bwho\s+won\b|\bscore\s+of\s+the\b|\b(?:red\s+sox|patriots|celtics|bruins|yankees|super\s+bowl|"
               r"world\s+series|nfl|nba|mlb|nhl|fantasy\s+football|playoffs?)\b(?![-\s]?\d)"),
    ("politics", r"\b(?:election|president|democrats?|republicans?|politic\w*|congress|senator|vote\s+for|who\s+to\s+vote|"
                 r"religion|god\s+exist)\b"),
    ("money", r"\b(?:stock\s+market|stocks?\s+(?:to\s+buy|tips?|picks?)|crypto\w*|bitcoin|lottery|gambl\w*|bet\s+on|"
              r"invest(?:ing|ment)\s+advice|should\s+i\s+(?:buy|sell)\s+(?:bitcoin|stocks?|shares))\b"),
    ("trivia", r"\b(?:capital\s+of|homework|solve\s+(?:this|for\s+x)|what\s+year\s+did|who\s+(?:invented|was\s+the\s+first|"
               r"is\s+the\s+president)|meaning\s+of\s+life|how\s+many\s+(?:planets|countries|states\s+in))\b"),
    ("about_me", r"\b(?:are\s+you\s+(?:human|real|a\s+real|alive|sentient|a\s+robot|a\s+person|single|married)|"
                 r"do\s+you\s+have\s+feelings|tell\s+me\s+about\s+yourself|how\s+old\s+are\s+you|what['’]?s\s+your\s+name|"
                 r"do\s+you\s+love\s+me|how\s+are\s+you\s+(?:today|doing))\b"),
    ("creative", r"\b(?:jokes?|poems?|sonnet|haiku|limerick|riddle|lyrics|story|stories|essay|rap|fan\s?fiction)\b"),
    ("entertainment", r"\b(?:movies?|netflix|tv\s+shows?|songs?|concert|video\s+games?|horoscope|zodiac|celebrit\w*)\b"),
    ("travel", r"\b(?:flights?\s+to|hotels?\s+in|vacation|things\s+to\s+do\s+in|travel\s+to|trip\s+to)\b"),
    ("health", r"\b(?:headache|my\s+(?:back|head|knee|stomach)\s+hurts|medicine\s+for|lose\s+weight|diet\s+plan|"
               r"am\s+i\s+sick|covid|flu\s+symptoms)\b"),
]
_OFF_RX = [(k, re.compile(rx, re.I)) for k, rx in _OFF]
# words that make it a Webb question whatever else it borrows ("heat pump for cold weather", "tie into a gas line")
_TRADE_STRONG = re.compile(
    r"\b(?:water\s+heaters?|heaters?|boilers?|furnaces?|valves?|pipes?|piping|fittings?|pex|copper|tanks?|pumps?|"
    r"thermostats?|faucets?|toilets?|sinks?|condensers?|flanges?|kits?|unions?|separators?|couplings?|elbows?|tees?|"
    r"adapters?|nipples?|sweat|npt|heat\s+pumps?|mini[\s-]?splits?|ducts?|ductwork|vents?|venting|"
    r"flue|gas|propane|breakers?|wir(?:e|es|ing)|capacitors?|compressors?|motors?|blowers?|parts?|model|items?|skus?|"
    r"orders?|invoices?|branch(?:es)?|webb|deliver\w*|quotes?|cart|warrant\w*|install\w*|repair\w*|plumb\w*|hvac|btu|"
    r"psi|gpm|sump|drains?|softeners?|filters?|hoses?|fixtures?|showers?|tubs?|refrigerant|a/?c|air\s+condition\w*|"
    r"insulation|radiant|snow\s+melt|hangers?|straps?|anode|element|igniter|flame|pilot|serial|date\s+code|"
    r"humidifier|dehumidifier|generator|kitchen|bathroom|laundry|garage|crawl\s?space|basement|attic|roof|"
    r"jobs?|contractor|customer|tools?|drill|saw|catalog|stock\s+of|in\s+stock|price|account)\b", re.I)
_CRISIS = re.compile(r"\b(?:suicid\w*|kill(?:ing)?\s+myself|end\s+(?:my|it\s+all)\s+life|end\s+it\s+all|hurt(?:ing)?\s+"
                     r"myself|self[\s-]harm|want\s+to\s+die|don['’]?t\s+want\s+to\s+(?:live|be\s+alive))\b", re.I)
_THREAT = re.compile(r"\b(?:threaten\w*\s+(?:me|us|my)|(?:is|are|keeps)\s+threatening|stalk\w*|restraining\s+order|"
                     r"(?:he|she|they)\s+(?:hit|beat|hurt)s?\s+me|kill\s+(?:him|her|them)|i['’]?m\s+(?:scared|afraid)\s+"
                     r"(?:of|for)\s+my\s+(?:life|safety))\b", re.I)


def off_topic(question: str) -> tuple[str, str] | None:
    """(kind, reply) for a question with nothing to do with F.W. Webb, else None. A question that names a product, an
    order or a job is never off topic, whatever else it borrows."""
    q = question or ""
    if _TRADE_STRONG.search(q):
        return None
    for kind, rx in _OFF_RX:
        if rx.search(q):
            return kind, _pick(kind, q)
    return None


def _pick(kind: str, q: str) -> str:
    import hashlib
    opts = OFF_TOPIC[kind]
    return opts[int(hashlib.sha1(re.sub(r"\W+", " ", q.lower()).strip().encode()).hexdigest(), 16) % len(opts)]


def off_topic_reply(question: str) -> str:
    """The light reply for any question the scope check turned away (the general kind when no topic is recognized)."""
    hit = off_topic(question)
    return hit[1] if hit else _pick("general", question or "")


# personal data
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"(?<![\w#])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")


def _luhn(digits: str) -> bool:
    s, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        s += d
        alt = not alt
    return s % 10 == 0


def redact(text: str) -> str:
    t = text or ""
    t = _CARD.sub(lambda m: "[card]" if _luhn(re.sub(r"\D", "", m.group(0))) else m.group(0), t)
    t = _SSN.sub("[ssn]", t)
    t = _EMAIL.sub("[email]", t)
    return _PHONE.sub("[phone]", t)


def _webb_domain(d: str) -> bool:
    d = d.lower().rstrip(".")
    return any(d == w or d.endswith("." + w) for w in WEBB_DOMAINS)


def has_pii(text: str) -> bool:
    return redact(text) != (text or "")


# ------------------------------------------------------------------ the question
@dataclass
class Inspection:
    kind: str | None = None             # a set reply: pricing, complaint, legal, injection, abusive, where_to_buy
    reply: str | None = None
    flags: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def review(self) -> bool:
        return bool(set(self.flags) & {"warranty", "repair", "lifecycle", "complaint", "legal", "recall", "injection",
                                       "abusive", "pricing", "damage"})

    def as_dict(self) -> dict:
        return {"kind": self.kind, "flags": self.flags, "notes": self.notes, "review": self.review}


def inspect(question: str, branch: str | None = None, *, price_lookup_ok: bool = True) -> Inspection:
    """`price_lookup_ok`: "how much is X" is a fair question for Ask Webb (it shows the account's price) but the
    Knowledge Center answers from documents and sends price questions to the product page and the branch."""
    q = question or ""
    i = Inspection()
    f = i.flags
    if _PROFANE.search(q):
        f.append("profanity")
    if has_pii(q):
        f.append("pii")
    for name, rx in (("warranty", _WARRANTY), ("repair", _REPAIR), ("lifecycle", _LIFECYCLE), ("code", _CODE),
                     ("recall", _RECALL), ("damage", _DAMAGE)):
        if rx.search(q):
            f.append(name)
    i.notes = [NOTES[n] for n in ("warranty", "lifecycle", "code", "recall") if n in f]
    kind = None
    off = None
    if _CRISIS.search(q) or _THREAT.search(q):
        kind = "personal_safety"
    elif _INJECTION.search(q):
        kind = "injection"
    elif _LEGAL.search(q):
        kind = "legal"
    elif _NEG_WEBB_IN.search(q):
        kind = "complaint"
    elif "profanity" in f and _AT_YOU.search(q) and not re.search(r"\b(?:won['’]?t|doesn['’]?t|keeps|isn['’]?t|"
                                                                   r"stopped|broken|leak\w*)\b", q, re.I):
        kind = "abusive"
    elif (off := off_topic(q)):
        kind = "off_topic"
    elif _PRICE_TALK_IN.search(q) or (not price_lookup_ok and _PRICE_LOOKUP.search(q)):
        kind = "pricing"
    elif _WHERE_IN.search(q) and not _WHERE_NOT.search(q):
        kind = "where_to_buy"
    if kind:
        i.kind = kind
        f.append(kind)
        i.reply = WHERE_REPLY.format(branch=branch or "your F.W. Webb branch") if kind == "where_to_buy" \
            else off[1] if kind == "off_topic" else SAFETY_REPLY if kind == "personal_safety" else SET_REPLIES[kind]
    return i


# ------------------------------------------------------------------ the answer
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9#\"“(])")
_ITEM = re.compile(r"#(\d{6,8})\b")
_ITEM_WORD = re.compile(r"\b(?:webb\s+)?(?:item|sku|part)\s*(?:#|no\.?|number)?\s*(\d{6,8})\b", re.I)
_MODELISH = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{3,}\b", re.I)
# spec words and sizes that look like model numbers but aren't: R-410A, E04, SEER2, 50GAL, 24V, 2X4, NPT
_NOT_MODEL = re.compile(r"R-?\d{2,3}[A-Z]?|E-?\d{1,3}|(?:SEER|EER|HSPF)\d?|\d+(?:\.\d+)?[A-Z]{1,4}|\d+X\d+(?:X\d+)?|"
                        r"[A-Z]{1,4}-?\d|(?:UL|NSF|CSA|AHRI|ASME|ANSI)-?\d*", re.I)
_UNITS = (r"gpm|gph|psi|btu\w*|mbh|gal\w*|tons?|seer2?|eer2?|hspf2?|afue|amps?|a|v|vac|volts?|w|watts?|kw|"
          r"in(?:ch(?:es)?)?\.?|ft|feet|years?|yrs?|months?|%|°f|°c|db|dba|lbs?|pounds?|cfm|kg|rpm|ohms?|mfd|uf|µf|va|kva|"
          r"hp|liters?|qts?|oz|mm|cm")
_QTY = re.compile(rf"(?<![\w.#$])(\d+(?:[.,]\d+)*)\s*({_UNITS})\b", re.I)
_SPELLED_QTY = re.compile(rf"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
                          rf"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
                          rf"hundred|thousand|dozen)[\s-]+(?:{_UNITS}|decades?)\b", re.I)
_TIME = {"year": "yr", "years": "yr", "yr": "yr", "yrs": "yr", "month": "mo", "months": "mo"}


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _source_numbers(sources: str) -> set[float]:
    return {v for v in (_num(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", sources or "")) if v is not None}


def _source_spans(sources: str) -> set[tuple[float, str]]:
    """Durations the sources state: "6 yr", "10-year", or a JSON field like "warranty_years": 6."""
    out = set()
    for m in re.finditer(r"(\d+(?:\.\d+)?)[\s-]*(years?|yrs?|months?)\b", sources or "", re.I):
        out.add((float(m.group(1)), _TIME[m.group(2).lower()]))
    for m in re.finditer(r'"([a-z_]*(?:year|yr|warranty|month)[a-z_]*)"\s*:\s*"?(\d+(?:\.\d+)?)', sources or "", re.I):
        out.add((float(m.group(2)), "mo" if "month" in m.group(1).lower() else "yr"))
    return out


def ungrounded(text: str, sources: str, item_ok=None) -> list[str]:
    """What an answer states that its sources (tool results, document pages, the question) never did: item numbers
    (with or without #), model numbers, dollar amounts, quantities with units (spelled out too), and durations (a
    number of years has to be a number of years in the sources, not a stock count). Counting words are allowed.
    `item_ok(sku)`: an item number the catalog has is grounded even when no tool returned it."""
    t = text or ""
    src = sources or ""
    src_u, src_l = src.upper(), src.lower()
    nums = _source_numbers(src)
    out = []
    items = {m.group(1) for m in _ITEM.finditer(t)} | {m.group(1) for m in _ITEM_WORD.finditer(t)}
    for sku in items:
        if sku not in src and not (item_ok and item_ok(sku)):
            out.append("#" + sku)
    for m in _MODELISH.finditer(t):
        tok = m.group(0)
        if tok.isdigit() or tok in items:
            continue
        if tok.upper() not in src_u and not _NOT_MODEL.fullmatch(tok):
            out.append(tok)
    for m in _AMOUNT.finditer(t):
        v = _num(m.group(1))
        if v is None or not any(abs(v - n) < 0.006 for n in nums):
            out.append("$" + m.group(1))
    spans = None
    for m in _QTY.finditer(t):
        v = _num(m.group(1))
        if v is None:
            continue
        unit = _TIME.get(m.group(2).lower())
        if unit:
            spans = _source_spans(src) if spans is None else spans
            if (v, unit) not in spans:
                out.append(m.group(0))
        elif v not in nums:
            out.append(m.group(0))
    for m in _SPELLED_QTY.finditer(t):
        if m.group(1).lower() not in src_l:
            out.append(m.group(0))
    return out


def violations(text: str, *, sources: str | None = None, allow_amounts: bool = False) -> list[str]:
    """What's wrong with one answer (or one sentence of it). `sources`: the text the answer may draw on; with it, a
    claim or a number the sources make is allowed. `allow_amounts`: a dollar amount is data (an invoice balance, the
    account's price) when the sources hold it."""
    t = text or ""
    why = []
    if _PROFANE.search(t):
        why.append("profanity")
    if _NEG_WEBB_OUT.search(t):
        why.append("negative_webb")
    if _PRICE_OUT.search(t) or (_AMOUNT.search(t) and not allow_amounts):
        why.append("pricing")
    if _ELSEWHERE.search(t) or any(not _webb_domain(d) for d in _URL.findall(t)):
        why.append("elsewhere")
    for m in _CLAIM.finditer(t):
        if sources is None or not _claim_in_sources(t, m.group(0), sources):
            why.append("claim")
            break
    if sources is not None and ungrounded(t, sources):
        why.append("ungrounded")
    return why


def _claim_in_sources(sentence: str, claim: str, sources: str) -> bool:
    """A claim is the source's when the source makes it too (same wording) and every number in the sentence is in it."""
    s = sources.lower()
    key = re.sub(r"\s+", " ", claim.lower()).strip()
    if key not in s:
        return False
    have = _source_numbers(sources)
    return all(v in have for v in (_num(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", sentence)) if v is not None)


def sentences(text: str) -> list[str]:
    out = []
    for para in (text or "").split("\n"):
        out.append([s for s in _SENT.split(para) if s.strip()])
    return out


def scrub(text: str, *, sources: str | None = None, allow_amounts: bool = False) -> tuple[str, list[str]]:
    """The answer without the sentences that break a rule, paragraphs kept; and what was removed and why."""
    kept_paras, why = [], []
    for para in sentences(text):
        keep = []
        for s in para:
            v = violations(s, sources=sources, allow_amounts=allow_amounts)
            if v:
                why += v
            else:
                keep.append(s)
        kept_paras.append(" ".join(keep))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_paras)).strip()
    return out, list(dict.fromkeys(why))
