"""LangGraph state schema."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

MAX_RETRIES = 2


class AgentState(TypedDict, total=False):
    video_url: str
    video_id: str
    metadata: Dict[str, Any]
    channel_history: Dict[str, Any]
    transcript_summary: Dict[str, Any]
    extracted_claims: List[Dict[str, Any]]
    comment_insights: List[Dict[str, Any]]  # new information found in viewer comments
    community_insights: List[Dict[str, Any]]  # outside discussion (web/social), each with a real URL
    verification_results: List[Dict[str, Any]]
    critic_feedback: List[Dict[str, Any]]
    retry_count: int
    error_message: Optional[str]
    overall_accuracy_score: int
    saved_to_memory: bool
    report: Dict[str, Any]  # structured report v2 (see agent/report.py)
    final_report_md: str
