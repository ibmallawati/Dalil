"""
registry.py — D6 Layer 1: the deterministic scope pre-gate.

Runs BEFORE embedding. Given a question, it answers one thing: is this
inside Daleel's declared coverage?

--- Why this sits in front of retrieval, not behind it -------------------

Measurement on multilingual-e5-base showed the in-corpus and out-of-corpus
similarity distributions separated by only ~0.015: the worst legitimate
question scored 0.7968, the best completely unrelated one ("what are the
best restaurants in Muscat?") scored 0.7816. No absolute threshold can
cleanly separate those, and tuning will not create a gap that isn't there.

So the refusal decision is not left to a similarity score. A keyword match
against a declared coverage map is exact, explainable, costs nothing, and
cannot drift. When it fires there is no reason to embed anything at all.

--- The three verdicts, and why UNKNOWN is not a refusal -----------------

COVERED               a covered authority or one of its services was named
                      -> proceed to retrieval
UNCOVERED_AUTHORITY   a DIFFERENT, known Omani authority was named
                      -> refuse now, name it, say it is coming
UNKNOWN               nothing matched either way
                      -> proceed to retrieval anyway

UNKNOWN deliberately falls through rather than refusing. Keyword matching
has no opinion about «كيف أحصل على وثيقة رسمية؟», and a gate that refuses
whatever it fails to recognise would reject valid questions phrased in
words nobody thought to list. Layers 2 and 3 handle those. This layer only
speaks when it is certain.

--- Adding an authority is a data operation ------------------------------

Everything this module knows comes from data/registry/authorities.json.
Extending coverage means editing that file and adding corpus files. That
claim is made to the panel; this module is what keeps it honest.
"""

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from chatbot.config import DATA_DIR
from chatbot.rag.arabic import normalize_arabic

logger = logging.getLogger(__name__)

REGISTRY_FILE = DATA_DIR / "registry" / "authorities.json"

# Below this length a "variant" matches too much to be evidence of anything.
MIN_VARIANT_LEN = 4


class Verdict(Enum):
    COVERED = "covered"
    UNCOVERED_AUTHORITY = "uncovered_authority"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RegistryMatch:
    verdict: Verdict
    authority_id: Optional[str] = None
    authority_ar: Optional[str] = None
    authority_en: Optional[str] = None
    portal_url: Optional[str] = None
    service_id: Optional[str] = None
    service_ar: Optional[str] = None
    matched_on: Optional[str] = None      # the variant that fired, for logs
    candidate_service_ids: tuple = field(default_factory=tuple)
    # Set when the question ALSO named an authority we don't cover, while
    # still naming something we do. The covered part wins; this is carried
    # so the answer can footnote the rest rather than pretend it wasn't
    # asked. See classify()'s precedence rule.
    also_mentioned_uncovered_ar: Optional[str] = None
    # D4: populated only when the question hit a declared ambiguous phrase
    # that its disambiguators did not resolve.
    clarify_question_ar: Optional[str] = None
    clarify_options: tuple = field(default_factory=tuple)

    @property
    def should_retrieve(self) -> bool:
        """UNCOVERED_AUTHORITY is the only verdict that skips retrieval."""
        return self.verdict is not Verdict.UNCOVERED_AUTHORITY

    @property
    def needs_clarification(self) -> bool:
        """More than one covered service is genuinely in play."""
        return bool(self.clarify_options)


def _load():
    with open(REGISTRY_FILE, encoding="utf-8") as f:
        return json.load(f)


_REGISTRY = _load()


def _prepare(variants):
    """Normalize once, drop the too-short, longest first (most specific wins)."""
    out = []
    for v in variants:
        n = normalize_arabic(v).lower()
        if len(n) >= MIN_VARIANT_LEN:
            out.append((n, v))
    return sorted(out, key=lambda p: -len(p[0]))


# Precomputed at import so a request does no JSON parsing or normalization
# of the registry itself.
_COVERED = []
for _a in _REGISTRY["covered"]:
    _COVERED.append({
        "id": _a["id"],
        "name_ar": _a["name_ar"],
        "name_en": _a["name_en"],
        "portal_url": _a["portal_url"],
        "variants": _prepare(_a["variants"]),
        "services": [
            {
                "service_id": _s["service_id"],
                "name_ar": _s["name_ar"],
                "variants": _prepare(_s["variants"]),
            }
            for _s in _a["services"]
        ],
    })

_NOT_COVERED = [
    {
        "name_ar": _a["name_ar"],
        "name_en": _a["name_en"],
        "variants": _prepare(_a["variants"]),
    }
    for _a in _REGISTRY["not_covered"]
]

_AMBIGUOUS = [
    {
        "id": _p["id"],
        "authority_id": _p["authority_id"],
        "question_ar": _p["question_ar"],
        "triggers": _prepare(_p["trigger_phrases"]),
        "options": [
            {
                "label_ar": _o["label_ar"],
                "service_id": _o["service_id"],
                # Word-boundary matched, so «جديد» cannot fire inside «تجديد».
                "disambiguators": [normalize_arabic(d).lower() for d in _o["disambiguators"]],
            }
            for _o in _p["options"]
        ],
    }
    for _p in _REGISTRY.get("ambiguous_phrases", [])
]

logger.info(
    "registry_loaded",
    extra={
        "covered_authorities": len(_COVERED),
        "covered_services": sum(len(a["services"]) for a in _COVERED),
        "known_uncovered": len(_NOT_COVERED),
    },
)


def _find(haystack, variants):
    """
    Return (normalized_variant, original) of the first (longest) hit.

    Substring matching, not word-boundary: Arabic attaches prefixes to the
    word (ب/ل/ال/و), so «للجواز» must still hit the variant «الجواز». The
    MIN_VARIANT_LEN floor is what keeps that from over-matching.
    """
    for norm, original in variants:
        if norm in haystack:
            return norm, original
    return None


def _has_word(haystack, phrase):
    """
    Word-boundary containment, used ONLY for disambiguators.

    Disambiguators decide whether to ask the citizen a question, so a false
    positive silently skips a clarification that was needed. «جديد» must not
    match inside «تجديد» — which substring matching would do, and which
    would resolve "renew my CR" to "register a new CR", exactly backwards.
    """
    return f" {phrase} " in f" {haystack} "


def _drop_subsumed(matches):
    """
    Remove service matches whose variant is contained in a longer one.

    «رخصتي» (driving licence) is a substring of «رخصتي التجارية» (trade
    licence), so «أريد تجديد رخصتي التجارية» hits both. That is one phrase
    matched twice, not two services in play, and treating it as ambiguity
    would make Daleel ask a pointless question.
    """
    kept = []
    for authority, service, hit in matches:          # already longest-first
        if any(hit[0] in prev[2][0] for prev in kept):
            continue
        kept.append((authority, service, hit))
    return kept


def classify(question: str) -> RegistryMatch:
    """
    Decide whether `question` is inside Daleel's declared coverage.

    Matching runs on D3-normalized text on both sides, so «الهوية» and
    «الهويه» are the same string by the time they get here — the registry
    file does not have to enumerate spelling variants that D3 already folds.
    """
    haystack = normalize_arabic(question or "").lower()
    if not haystack:
        return RegistryMatch(Verdict.UNKNOWN)

    # Always resolved, even when something covered also matches, so a
    # COVERED answer can footnote the part it isn't handling.
    uncovered_hit = None
    for other in _NOT_COVERED:
        hit = _find(haystack, other["variants"])
        if hit:
            uncovered_hit = (other, hit)
            break

    # A covered authority named outright.
    best_authority = None
    for authority in _COVERED:
        hit = _find(haystack, authority["variants"])
        if hit and (best_authority is None or len(hit[0]) > len(best_authority[1][0])):
            best_authority = (authority, hit)

    # A covered SERVICE named, with or without its authority. Most citizens
    # write «كيف أجدد جوازي؟» and never name the ROP at all, so service
    # matching carries most of the weight in practice.
    matched_services = []
    for authority in _COVERED:
        for service in authority["services"]:
            hit = _find(haystack, service["variants"])
            if hit:
                matched_services.append((authority, service, hit))
    matched_services.sort(key=lambda t: -len(t[2][0]))   # longest variant first
    matched_services = _drop_subsumed(matched_services)

    # A declared ambiguity its disambiguators could not resolve. Checked
    # before the single-service path, because «تجديد السجل التجاري» matches
    # exactly one variant yet is genuinely two different requests.
    clarify = _resolve_ambiguity(haystack)

    # --- PRECEDENCE -----------------------------------------------------
    # COVERED always beats UNCOVERED_AUTHORITY. «أريد تجديد جواز سفري ثم
    # مراجعة وزارة الصحة» names something Daleel can help with; refusing the
    # whole question because a second, uncovered authority appeared would
    # withhold an answer it actually has.

    if clarify:
        authority = next(a for a in _COVERED if a["id"] == clarify["authority_id"])
        return RegistryMatch(
            verdict=Verdict.COVERED,
            authority_id=authority["id"],
            authority_ar=authority["name_ar"],
            authority_en=authority["name_en"],
            portal_url=authority["portal_url"],
            matched_on=clarify["matched_on"],
            candidate_service_ids=tuple(o["service_id"] for o in clarify["options"]),
            clarify_question_ar=clarify["question_ar"],
            clarify_options=tuple(
                {"label_ar": o["label_ar"], "service_id": o["service_id"]}
                for o in clarify["options"]
            ),
            also_mentioned_uncovered_ar=uncovered_hit[0]["name_ar"] if uncovered_hit else None,
        )

    if matched_services:
        authority, service, hit = matched_services[0]
        return RegistryMatch(
            verdict=Verdict.COVERED,
            authority_id=authority["id"],
            authority_ar=authority["name_ar"],
            authority_en=authority["name_en"],
            portal_url=authority["portal_url"],
            service_id=service["service_id"],
            service_ar=service["name_ar"],
            matched_on=hit[1],
            candidate_service_ids=tuple(s["service_id"] for _, s, _ in matched_services),
            also_mentioned_uncovered_ar=uncovered_hit[0]["name_ar"] if uncovered_hit else None,
        )

    if best_authority:
        authority, hit = best_authority
        return RegistryMatch(
            verdict=Verdict.COVERED,
            authority_id=authority["id"],
            authority_ar=authority["name_ar"],
            authority_en=authority["name_en"],
            portal_url=authority["portal_url"],
            matched_on=hit[1],
            also_mentioned_uncovered_ar=uncovered_hit[0]["name_ar"] if uncovered_hit else None,
        )

    # A different Omani authority we know about but do not cover, and
    # nothing covered alongside it. Only THIS refuses without retrieving.
    if uncovered_hit:
        other, hit = uncovered_hit
        return RegistryMatch(
            verdict=Verdict.UNCOVERED_AUTHORITY,
            authority_ar=other["name_ar"],
            authority_en=other["name_en"],
            matched_on=hit[1],
        )

    # No opinion. Not a refusal — let the later layers judge.
    return RegistryMatch(Verdict.UNKNOWN)


def _resolve_ambiguity(haystack):
    """
    Return clarification data when a declared ambiguous phrase fired and
    its disambiguators did not settle it. None otherwise.
    """
    for entry in _AMBIGUOUS:
        hit = _find(haystack, entry["triggers"])
        if not hit:
            continue
        resolved = [
            option for option in entry["options"]
            if any(_has_word(haystack, d) for d in option["disambiguators"])
        ]
        if len(resolved) == 1:
            return None          # the citizen already said which one
        return {
            "authority_id": entry["authority_id"],
            "question_ar": entry["question_ar"],
            "options": entry["options"],
            "matched_on": hit[1],
        }
    return None


def uncovered_authority_message(match: RegistryMatch) -> str:
    """D6 refusal path 2: we know this authority, we just don't cover it yet."""
    return (
        f"{match.authority_ar} ليست ضمن قاعدة معرفتي بعد — سيتم إضافتها قريبًا.\n\n"
        "أستطيع حاليًا المساعدة في خدمات شرطة عمان السلطانية "
        "ووزارة التجارة والصناعة وترويج الاستثمار."
    )


def unknown_service_message(match: RegistryMatch) -> str:
    """
    D6 refusal path 1: the authority IS covered, this specific procedure is
    not. Different from path 2, and the citizen is owed the difference —
    plus the official portal, so the answer is still useful.
    """
    authority = match.authority_ar or "هذه الجهة"
    text = f"ليس لدي تفاصيل هذه الخدمة تحديدًا ضمن ما تم توثيقه من خدمات {authority}."
    if match.portal_url:
        text += f"\n\nيمكنك مراجعة البوابة الرسمية: {match.portal_url}"
    return text


def coverage_summary():
    """
    Used for /healthz and for stating scope to the panel: "we cover N
    authorities and M services today; adding one is a data operation."
    """
    return {
        "authorities": [
            {
                "id": a["id"],
                "name_ar": a["name_ar"],
                "name_en": a["name_en"],
                "services": [s["service_id"] for s in a["services"]],
            }
            for a in _COVERED
        ],
        "authority_count": len(_COVERED),
        "service_count": sum(len(a["services"]) for a in _COVERED),
        "known_uncovered_count": len(_NOT_COVERED),
    }
