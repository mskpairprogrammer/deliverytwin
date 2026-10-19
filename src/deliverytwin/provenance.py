"""Where each SHAPE fact comes from, checked by code rather than trusted from the model.

Every extracted fact, risk or question rationale carries a source badge:

* Stated in Request: a quote that really appears in the request text.
* Found in Meeting Notes / ROM Document (Quote Verified): a quote that really appears in that uploaded document.
* AI Inference (Confirm): anything else, including every claim whose quote is missing or cannot be found.

The check is a deterministic substring test after normalising whitespace and case. The model's claim about
where something came from is never shown as verified unless this test passes.
"""
from __future__ import annotations

import re
from typing import Any

MIN_QUOTE_WORDS = 3
MAX_QUOTE_CHARS = 400
MAX_FINDINGS = 8
MAX_FINDING_TEXT = 240

LABELS = {
    "request": "Stated in Request",
    "meeting_notes": "Found in Meeting Notes (Quote Verified)",
    "rom": "Found in ROM Document (Quote Verified)",
    "inference": "AI Inference (Confirm)",
    "rules": "Rules-based suggestion (Confirm)",
}
FINDING_KINDS = ("fact", "risk", "dependency")

_RISK_WORDS = re.compile(
    r"\b(risks?|blocked?|blockers?|delay\w*|not yet agreed|no shared test|book(?:s)? out|time away|disputed?|usually takes)\b",
    re.I,
)
_DEPENDENCY_WORDS = re.compile(r"\b(dependenc\w+|depends?|approval|privacy review|consent|cab|change advisory)\b", re.I)


def normalize(text: object) -> str:
    return " ".join(str(text or "").lower().split())


def verify_quote(quote: object, sources: list[str]) -> bool:
    """True when the quote (3+ words) appears word for word in one of the sources, ignoring case and whitespace."""
    cleaned = normalize(quote)
    if len(cleaned.split()) < MIN_QUOTE_WORDS:
        return False
    return any(cleaned in normalize(source) for source in sources)


def _inference(claimed: str | None, reason: str | None = None, quote: str | None = None) -> dict[str, Any]:
    return {
        "category": "inference", "label": LABELS["inference"], "document": None, "document_kind": None,
        "quote": quote, "verified": False, "claimed": claimed, "downgrade_reason": reason,
    }


def attribute(item: dict[str, Any], request: str, docs: list[dict[str, str]]) -> dict[str, Any]:
    """Resolve a claimed source (`source`, `source_document`, `source_quote`) into a verified badge or a downgrade."""
    claimed = item.get("source") if isinstance(item.get("source"), str) else None
    quote = item.get("source_quote")
    quote = " ".join(quote.split())[:MAX_QUOTE_CHARS] if isinstance(quote, str) and quote.strip() else None
    if claimed not in ("request", "document"):
        return _inference(claimed, None, None)
    if quote is None:
        return _inference(claimed, "no source quote was given", None)
    if claimed == "request":
        if verify_quote(quote, [request]):
            return {"category": "request", "label": LABELS["request"], "document": None, "document_kind": None,
                    "quote": quote, "verified": True, "claimed": claimed, "downgrade_reason": None}
        return _inference(claimed, "the quote is not in the request", quote)
    if not docs:
        return _inference(claimed, "no document was uploaded", quote)
    named = item.get("source_document")
    matching = [d for d in docs if isinstance(named, str) and d["name"].lower() == named.lower()]
    for document in matching or docs:
        if verify_quote(quote, [document.get("text", "")]):
            return {"category": "document", "label": LABELS[document["kind"]], "document": document["name"],
                    "document_kind": document["kind"], "quote": quote, "verified": True, "claimed": claimed,
                    "downgrade_reason": None}
    where = matching[0]["name"] if matching else "the uploaded documents"
    return _inference(claimed, f"the quote was not found word for word in {where}", quote)


def rules_attribution() -> dict[str, Any]:
    return {"category": "rules", "label": LABELS["rules"], "document": None, "document_kind": None, "quote": None,
            "verified": False, "claimed": None, "downgrade_reason": None}


def sentences(text: str) -> list[str]:
    """Sentences and bullet lines of a text, each a verbatim excerpt of it (headings and tables are skipped)."""
    found: list[str] = []
    for line in re.split(r"[\r\n]+", text):
        if re.match(r"^\s*(?:#|\|)", line):
            continue
        line = re.sub(r"^\s*(?:[-*>•]+|\d+[.)])\s*", "", line).strip()
        for part in re.split(r"(?:(?<=[.!?])|(?<=[.!?]\*\*))\s+(?=[A-Z0-9\"“*])", line):
            part = part.strip()
            if len(part.split()) >= 4:
                found.append(part)
    return found


def find_sentence(tokens: set[str], text: str, tokenize: Any) -> str | None:
    """The sentence of `text` sharing the most tokens with `tokens` (used to quote what a text already says)."""
    best, best_score = None, 0
    for sentence in sentences(text):
        score = len(tokens & tokenize(sentence))
        if score > best_score:
            best, best_score = sentence, score
    return best[:MAX_QUOTE_CHARS] if best else None


def clean_findings(raw: object, request: str, docs: list[dict[str, str]]) -> list[dict[str, Any]] | None:
    """Validate model findings; each gets a verified badge or a downgrade. None when nothing usable came back."""
    if not isinstance(raw, list):
        return None
    findings: list[dict[str, Any]] = []
    for item in raw[:MAX_FINDINGS]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            continue
        kind = item.get("kind") if item.get("kind") in FINDING_KINDS else "fact"
        findings.append({
            "kind": kind, "text": " ".join(item["text"].split())[:MAX_FINDING_TEXT],
            "source": attribute(item, request, docs), "origin": "ai",
        })
    return findings or None


def rules_findings(request: str, docs: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Findings picked by keyword from the request and uploaded documents; quotes are verified like any other.

    Documents take turns (meeting notes first) so one long document cannot crowd out the others.
    """
    candidates: list[tuple[str, dict[str, Any]]] = [
        ("fact", {"source": "request", "source_quote": sentence, "text": sentence}) for sentence in sentences(request)[:3]
    ]
    per_document: list[list[tuple[str, dict[str, Any]]]] = []
    for document in sorted(docs, key=lambda d: d["kind"] != "meeting_notes"):
        matches = []
        for sentence in sentences(document.get("text", "")):
            kind = "risk" if _RISK_WORDS.search(sentence) else "dependency" if _DEPENDENCY_WORDS.search(sentence) else None
            if kind:
                matches.append((kind, {"source": "document", "source_document": document["name"],
                                       "source_quote": sentence, "text": sentence}))
        per_document.append(matches)
    for rank in range(max((len(matches) for matches in per_document), default=0)):
        candidates.extend(matches[rank] for matches in per_document if rank < len(matches))
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, item in candidates:
        key = normalize(item["text"])
        if key in seen:
            continue
        seen.add(key)
        attribution = attribute(item, request, docs)
        if attribution["verified"]:
            findings.append({"kind": kind, "text": item["text"][:MAX_FINDING_TEXT], "source": attribution, "origin": "rules"})
        if len(findings) >= MAX_FINDINGS:
            break
    return findings


def source_counts(findings: list[dict[str, Any]], questions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"request": 0, "document": 0, "inference": 0, "rules": 0}
    for entry in [*findings, *questions]:
        counts[entry["source"]["category"]] += 1
    return counts
