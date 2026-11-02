"""Decide which items in a report the user already knows, from what they previously marked as known.

Cheap lexical matching narrows the candidates; one small LLM call judges whether a known entry states the SAME
knowledge. Nothing is pre-trained: the profile only contains what the user clicked "I already know this" on.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from agent import prompts
from agent.knowledge import KnowledgeStore, normalize_concept
from agent.schemas import KnownMatchOutput

log = logging.getLogger("yta.personalize")

_STOP = frozenset(
    "the a an and or of to in on for with is are was were be been it its this that these those as at by from how why what "
    "when which who not no can do does did has have had you your they their about into than then so if more most".split()
)
MAX_CANDIDATES = 60
PER_ITEM_CANDIDATES = 8

_cache: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}


def tokens(text: str) -> set:
    """Lowercased content words with naive plural stripping."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) > 2 and w not in _STOP:
            out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def learnable_items(report: Dict[str, Any]) -> List[Dict[str, str]]:
    """The items a user can mark as known: key points and comment insights."""
    items = [{"id": kp["id"], "concept": kp["concept"], "text": kp["point"]} for kp in report["summary"]["key_points"]]
    items += [{"id": c["id"], "concept": c["concept"], "text": c["detail"]} for c in report["comments"]]
    return items


def _candidates(item: Dict[str, str], entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    item_tokens = tokens(item["concept"]) | tokens(item["text"])
    item_concept = tokens(item["concept"])
    scored = []
    for e in entries:
        shared = item_tokens & (tokens(e["concept"]) | tokens(e["text"]))
        concept_shared = item_concept & tokens(e["concept"])
        if len(shared) >= 2 or concept_shared:
            scored.append((len(shared) + 2 * len(concept_shared), e))
    scored.sort(key=lambda p: -p[0])
    return [e for _, e in scored[:PER_ITEM_CANDIDATES]]


async def personalize_report(
    report: Dict[str, Any], store: KnowledgeStore, llm: Any, use_cache: bool = True
) -> Dict[str, Dict[str, Any]]:
    """Return {item_id: {"known": True, "entry_id", "concept"}} for items the user already knows."""
    entries = store.entries()
    if not entries:
        return {}
    key = (report["video_id"], store.version())
    if use_cache and key in _cache:
        return _cache[key]

    items = learnable_items(report)
    by_concept = {normalize_concept(e["concept"]): e for e in entries}
    known: Dict[str, Dict[str, Any]] = {}
    pending = []
    for it in items:  # exact concept match needs no model call
        hit = by_concept.get(normalize_concept(it["concept"]))
        if hit:
            known[it["id"]] = {"known": True, "entry_id": hit["id"], "concept": hit["concept"]}
        else:
            pending.append(it)

    candidates: Dict[str, Dict[str, Any]] = {}
    for it in pending:
        for e in _candidates(it, entries):
            candidates[e["id"]] = e
    candidate_list = list(candidates.values())[:MAX_CANDIDATES]
    judged = [it for it in pending if _candidates(it, entries)]

    if judged and candidate_list:
        try:
            human = (
                "NEW ITEMS:\n" + json.dumps([{k: it[k] for k in ("id", "concept", "text")} for it in judged], ensure_ascii=False)
                + "\n\nKNOWN ENTRIES:\n"
                + json.dumps([{k: e[k] for k in ("id", "concept", "text")} for e in candidate_list], ensure_ascii=False)
            )
            out: KnownMatchOutput = await llm.with_structured_output(KnownMatchOutput).ainvoke(
                [SystemMessage(content=prompts.PERSONALIZE_SYSTEM), HumanMessage(content=human)]
            )
            valid = {e["id"]: e for e in candidate_list}
            wanted = {it["id"] for it in judged}
            for m in out.matches:
                entry = valid.get(m.known_entry_id or "")
                if entry and m.item_id in wanted:
                    known[m.item_id] = {"known": True, "entry_id": entry["id"], "concept": entry["concept"]}
        except Exception:  # personalization is a convenience: on failure show everything as new
            log.exception("personalization failed; treating all items as new")
            return known

    _cache[key] = known
    if len(_cache) > 64:
        _cache.pop(next(iter(_cache)))
    return known


def clear_cache() -> None:
    _cache.clear()


def find_item(report: Dict[str, Any], item_id: str) -> Optional[Dict[str, str]]:
    return next((it for it in learnable_items(report) if it["id"] == item_id), None)
