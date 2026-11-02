"""Structured-output schemas the LLM must fill in (report v2)."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["Verified", "Mostly True", "Exaggerated", "Partially False", "Debunked", "Unverified"]
Importance = Literal["high", "medium", "low"]
VideoType = Literal["tutorial", "review", "news", "finance", "science", "opinion", "interview", "entertainment", "other"]


class ClickbaitBuster(BaseModel):
    verdict: str = Field(description="Direct 1-2 sentence English answer to the premise of the video title.")
    payoff_timestamp: str = Field(description="MM:SS (or H:MM:SS) where the host actually answers the title's premise.")
    clickbait_rating: Literal["Low", "Medium", "High"]


class KeyPoint(BaseModel):
    timestamp: str = Field(description="MM:SS from the transcript where the point is made.")
    point: str = Field(description="One self-contained sentence stating what the video teaches or reports, in English.")
    concept: str = Field(description="2-6 word canonical name of the idea (e.g. 'MCP client-server architecture'), used to recognise it in other videos.")
    importance: Importance
    claim_index: Optional[int] = Field(
        default=None, description="Index into `claims` when this point rests on a checkable claim; otherwise null."
    )


class Claim(BaseModel):
    timestamp: str = Field(description="MM:SS from the transcript where the claim is made.")
    claim: str = Field(description="The host's verifiable factual/statistical/numerical claim, in English.")
    importance: Importance = Field(description="high = the video's argument depends on it; low = a passing remark.")
    quantitative: bool = Field(description="True when the claim is arithmetic/financial/benchmark math that code can check.")


class SponsorMention(BaseModel):
    name: str
    timestamp_range: str = Field(default="", description="e.g. '02:10 - 03:45' if spoken in the video.")
    disclosed: bool = Field(description="Whether the host clearly disclosed the paid relationship.")


class ExtraItem(BaseModel):
    label: str = Field(default="", description="Optional short lead-in, e.g. a step number, product name, or date.")
    text: str


class ExtraSection(BaseModel):
    """A section chosen for THIS video; the UI renders it by `kind`."""

    title: str = Field(description="Reader-facing title, e.g. 'Steps to follow' or 'Durability concerns'.")
    kind: Literal["steps", "list", "comparison", "timeline", "callout"]
    items: List[ExtraItem]


class VideoAnalysis(BaseModel):
    video_type: VideoType
    tldr: str = Field(description="2-4 sentence English summary of what the video actually says, including the parts that are correct.")
    key_points: List[KeyPoint] = Field(description="The knowledge a viewer gains, in video order. Scale with video length (4-12).")
    clickbait: ClickbaitBuster
    claims: List[Claim] = Field(description="Importance-ranked CHECKABLE factual claims only.", max_length=8)
    sponsors: List[SponsorMention] = Field(default_factory=list)
    has_paid_promotion: bool = False
    disclosed_properly: bool = False
    bias_notes: str = Field(default="", description="Where the host's praise may be biased by sponsorship/affiliate links.")
    collaborators: List[str] = Field(default_factory=list)
    topic_tags: List[str] = Field(default_factory=list, description="1-4 lowercase topic tags.")
    extra_sections: List[ExtraSection] = Field(
        default_factory=list, description="0-3 sections that fit this video's type (steps, comparison, timeline...)."
    )


class CommentInsight(BaseModel):
    category: Literal["Tutorial Fix", "Free Alternative", "Reliability Report", "Correction", "Extra Context", "Other"]
    author: str = Field(description="The commenter's handle exactly as given.")
    likes: int = 0
    concept: str = Field(description="2-6 word canonical name of the idea.")
    detail: str = Field(description="What a viewer learns from this comment, one or two sentences, in English.")
    adds_information: bool = Field(
        description="True ONLY if the comment states a fact, fix, correction or alternative about the video's SUBJECT that a viewer can use. "
        "False for anything about the video/channel itself, requests, praise, personal anecdotes, questions, greetings and jokes."
    )


class CommentsAnalysis(BaseModel):
    insights: List[CommentInsight] = Field(
        default_factory=list, description="Only comments that ADD information (fixes, corrections, alternatives, long-term reports); no praise, jokes or questions."
    )


class OutsideItem(BaseModel):
    category: Literal["Tutorial Fix", "Free Alternative", "Reliability Report", "Correction", "Extra Context", "Other"]
    source: str = Field(description="Site or community, e.g. 'Hacker News' or 'Stack Overflow'.")
    url: str = Field(description="The exact URL returned by a tool for this item.")
    detail: str


class ClaimVerdict(BaseModel):
    timestamp: str
    claim: str
    verdict: Verdict
    evidence: str = Field(description="1-3 sentences of evidence grounded ONLY in retrieved sources, quoting the specific figure found.")
    sources: List[str] = Field(description="URLs actually returned by tools. Empty only when verdict is Unverified.")
    confidence: Literal["high", "medium", "low"]


class VerificationOutput(BaseModel):
    results: List[ClaimVerdict]
    outside_discussion: List[OutsideItem] = Field(default_factory=list)


class WeakClaim(BaseModel):
    claim: str = Field(description="Exact claim text from the weak result.")
    suggested_query: str = Field(description="A better, more specific web search query.")
    reason: str = Field(default="", description="One sentence: why the earlier search fell short and how this query fixes it.")


class CriticOutput(BaseModel):
    weak_claims: List[WeakClaim]


class KnownMatch(BaseModel):
    item_id: str
    known_entry_id: Optional[str] = Field(
        default=None, description="Id of the user's known entry that covers the SAME knowledge, or null."
    )


class KnownMatchOutput(BaseModel):
    matches: List[KnownMatch]
