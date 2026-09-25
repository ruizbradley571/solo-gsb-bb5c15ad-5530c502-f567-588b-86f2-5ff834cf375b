"""Transcript typo batch-correction engine (转写错词批量校正).

The user supplies several wrong-word -> correct-word pairs; the engine scans
the *saved* transcript segment texts and previews every hit per segment.

Matching semantics:

* pairs are compared after stripping surrounding whitespace; a completely
  blank row is ignored, while a blank wrong word / blank correct word or a
  duplicated wrong word blocks the operation (ValueError);
* at the same scan position the **longest** wrong word wins (``计算机`` wins
  over ``计算``);
* each segment's original text is scanned exactly once, left to right, and
  replacement output is never re-matched (so ``a -> b`` followed by
  ``b -> c`` cannot chain inside one run);
* case sensitivity is configurable (default: exact / case-sensitive);
* a pair whose correct word equals its wrong word never produces a hit.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_pairs(pairs: list[dict]) -> list[dict]:
    """Validate and normalize user-supplied correction pairs.

    Returns the de-duplicated, stripped ``[{from, to}]`` rows in their input
    order. Completely empty rows are dropped; raises ValueError with a
    user-facing message on a blank wrong word, blank correct word or
    duplicated wrong word.
    """
    normalized: list[dict] = []
    seen: set[str] = set()
    for raw in pairs or []:
        wrong = _clean(raw.get("from") if isinstance(raw, dict) else None)
        right = _clean(raw.get("to") if isinstance(raw, dict) else None)
        if not wrong and not right:
            continue  # completely empty row -> ignore
        if not wrong:
            raise ValueError("存在未填写错词的校正行，请补全或删除该行")
        if not right:
            raise ValueError(f"错词「{wrong}」的正词为空，请填写后再提交")
        if wrong in seen:
            raise ValueError(f"错词「{wrong}」配置了多条校正，请只保留一条")
        seen.add(wrong)
        normalized.append({"from": wrong, "to": right})
    return normalized


def _match_at(text: str, pos: int, wrongs: list[str],
              case_sensitive: bool) -> str | None:
    """Longest wrong word matching at ``pos`` (None when nothing matches)."""
    hay = text[pos:] if case_sensitive else text[pos:].lower()
    best: str | None = None
    for wrong in wrongs:
        needle = wrong if case_sensitive else wrong.lower()
        if hay.startswith(needle) and (best is None or len(needle) > len(best)):
            best = wrong
    return best


def correct_text(text: str, pairs: list[dict],
                 case_sensitive: bool = True) -> dict:
    """Scan one segment text once and apply the correction pairs.

    Returns ``{text, hits}`` where ``text`` is the corrected text and
    ``hits`` lists ``{from, to, start, end}`` ranges in the *original*
    text. Replacement output is never re-scanned and equal words never hit.
    """
    # An equal pair changes nothing; exclude it so it never masks another
    # pair that starts at the same position.
    mapping = {p["from"]: p["to"] for p in pairs if p["from"] != p["to"]}
    wrongs = sorted(mapping, key=len, reverse=True)
    hits: list[dict] = []
    if not isinstance(text, str) or not text or not wrongs:
        return {"text": text if isinstance(text, str) else "", "hits": hits}

    out: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        wrong = _match_at(text, pos, wrongs, case_sensitive)
        if wrong is None:
            out.append(text[pos])
            pos += 1
            continue
        right = mapping[wrong]
        hits.append({"from": wrong, "to": right,
                     "start": pos, "end": pos + len(wrong)})
        out.append(right)
        pos += len(wrong)  # scan continues after the original match only
    return {"text": "".join(out), "hits": hits}


def plan_correction(document: dict, pairs: list[dict],
                    case_sensitive: bool = True) -> dict:
    """Build the preview: per-segment hits, corrected text and statistics.

    Only segments with at least one hit are returned (in document order).
    Raises ValueError on invalid pairs (see :func:`normalize_pairs`).
    """
    normalized = normalize_pairs(pairs)
    segments = []
    per_pair: dict[str, dict] = {}
    total_hits = 0
    for index, seg in enumerate(document.get("segments", []) or []):
        if not isinstance(seg, dict):
            continue
        result = correct_text(seg.get("text", "") or "", normalized,
                              case_sensitive)
        hits = result["hits"]
        if not hits:
            continue
        for hit in hits:
            entry = per_pair.setdefault(
                hit["from"], {"from": hit["from"], "to": hit["to"],
                              "hits": 0, "segments": 0})
            entry["hits"] += 1
        hit_pairs = {h["from"] for h in hits}
        for wrong in hit_pairs:
            per_pair[wrong]["segments"] += 1
        total_hits += len(hits)
        segments.append({
            "index": index,
            "id": str(seg.get("id") or ""),
            "start": seg.get("start", 0.0),
            "end": seg.get("end", 0.0),
            "speaker": str(seg.get("speaker", "") or ""),
            "originalText": seg.get("text", "") or "",
            "correctedText": result["text"],
            "hits": hits,
        })

    pair_stats = sorted(per_pair.values(),
                        key=lambda e: (-e["hits"], e["from"]))
    return {
        "segments": segments,
        "pairs": pair_stats,
        "summary": {
            "pairs": len(normalized),
            "hitPairs": len(pair_stats),
            "hits": total_hits,
            "segments": len(segments),
        },
        "caseSensitive": case_sensitive,
    }


def transcript_version(document: dict) -> str:
    """Stable fingerprint of a transcript's segment texts for optimistic checks.

    Covers segment identity, time, speaker and text (everything a re-import
    or an edit can change). Annotations are deliberately excluded: editing
    an annotation never invalidates a correction preview.
    """
    payload = [
        [s.get("id"), s.get("start"), s.get("end"),
         s.get("speaker"), s.get("text")]
        for s in document.get("segments", []) or []
        if isinstance(s, dict)
    ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")).hexdigest()
    return f"segv-{digest[:20]}"
