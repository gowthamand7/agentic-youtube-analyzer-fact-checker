"""Structured-output schemas the LLM must fill in."""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field

Verdict = Literal["Verified", "Mostly True", "Exaggerated", "Partially False", "Debunked", "Unverified"]


class ClickbaitBuster(BaseModel):
    verdict: str = Field(description="Direct 1-2 sentence English answer to the premise of the video title.")
    payoff_timestamp: str = Field(description="MM:SS (or H:MM:SS) where the host actually answers the title's premise.")
    clickbait_rating: Literal["Low", "Medium", "High"]


class Claim(BaseModel):
    timestamp: str = Field(description="MM:SS from the transcript where the claim is made.")
    claim: str = Field(description="The host's verifiable factual/statistical/numerical claim, in English.")
    quantitative: bool = Field(description="True when the claim is arithmetic/financial/benchmark math that code can check.")


class SponsorMention(BaseModel):
    name: str
    timestamp_range: str = Field(default="", description="e.g. '02:10 - 03:45' if spoken in the video.")
    disclosed: bool = Field(description="Whether the host clearly disclosed the paid relationship.")


class Addition(BaseModel):
    category: Literal["Tutorial Fix", "Free Alternative", "Reliability Report", "Correction", "Other"]
    source: str = Field(description="e.g. 'YouTube Comment (@user)' or 'Reddit r/subreddit' or 'Hacker News'.")
    detail: str


class ExtractionOutput(BaseModel):
    clickbait: ClickbaitBuster
    claims: List[Claim] = Field(description="3-5 most important verifiable claims, ordered by importance.", max_length=5)
    sponsors: List[SponsorMention] = Field(default_factory=list)
    has_paid_promotion: bool
    disclosed_properly: bool
    bias_notes: str = Field(default="", description="Where the host's praise may be biased by sponsorship/affiliate links.")
    collaborators: List[str] = Field(default_factory=list)
    topic_tags: List[str] = Field(description="1-4 lowercase topic tags.")
    domain: str = Field(description="e.g. Tech Tutorial, Gadget Review, Finance/News, Health/Science, Other.")
    crowdsourced_additions: List[Addition] = Field(default_factory=list, description="From top viewer comments only.")
    takeaways: List[str] = Field(default_factory=list, description="Actionable checklist steps.")
    resources: List[str] = Field(default_factory=list, description="Links/tools/books mentioned.")


class ClaimVerdict(BaseModel):
    timestamp: str
    claim: str
    verdict: Verdict
    evidence: str = Field(description="1-3 sentences of evidence grounded ONLY in retrieved sources.")
    sources: List[str] = Field(description="URLs actually returned by tools. Empty only when verdict is Unverified.")
    confidence: Literal["high", "medium", "low"]


class VerificationOutput(BaseModel):
    results: List[ClaimVerdict]
    community_insights: List[Addition] = Field(default_factory=list)


class WeakClaim(BaseModel):
    claim: str = Field(description="Exact claim text from the weak result.")
    suggested_query: str = Field(description="A better, more specific web search query.")
    reason: str = Field(default="", description="One sentence: why the earlier search fell short and how this query fixes it.")


class CriticOutput(BaseModel):
    weak_claims: List[WeakClaim]
