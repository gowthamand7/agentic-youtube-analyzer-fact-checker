# System Design Document: Agentic YouTube Analyzer & Fact-Checking Platform

**Author:** System Architecture Team  
**Status:** Locked / Ready for Phase 1 Implementation  
**Architecture Pattern:** Stateful Multi-Tool Agent (LangGraph + Model Context Protocol)  
**Tech Stack:** Go (MCP Servers & Concurrency), Python (LangGraph Orchestrator & YouTube Ingestion), ClickHouse (OLAP Channel Memory), Docker (Persistence & Code Sandbox)

---

## 1. Executive Summary & Core Consumer Value

Traditional video summarizers fail viewers by blindly compressing a creator's transcript—repeating clickbait, glossing over undisclosed sponsorships, and ignoring factual errors. This platform is an **autonomous investigative AI agent** that treats a YouTube video not as ground truth, but as a set of claims to be audited against web evidence, community knowledge, and the creator's own historical track record.

### Consumer-Facing Value Pillars
1. **Clickbait Buster:** Answers the premise of the video title and thumbnail in the very first sentence, citing the exact timestamp where the host addresses the core topic.
2. **Sponsorship & Bias Alerts:** Inspects video descriptions, outbound affiliate links, and historical channel data to flag paid promotions, contrasting sponsor pitches against independent user complaints.
3. **Crowdsourced Additions:** Mines the YouTube comments section and external communities (Reddit, Hacker News, Bluesky) for tutorial fixes, long-term reliability reports, cheaper/free alternatives, and expert corrections.
4. **Autonomous Fact-Checking & Code Auditing:** Verifies statistical, scientific, and factual claims via agent-centric web search (Tavily) and an isolated Python sandbox for mathematical verification.
5. **Creator Accountability Score (Historical Memory):** Tracks every analyzed channel in an OLAP database (ClickHouse) to surface whether a creator's accuracy is trending up or down and which brands sponsor them most frequently.

---

## 2. High-Level System Architecture

Rather than pre-fetching transcripts and comments in a rigid deterministic pipeline, the system provides only the **YouTube URL** to a stateful **LangGraph Agent**. The agent autonomously plans its investigation and invokes decoupled **Model Context Protocol (MCP)** servers on demand over JSON-RPC (`stdio` / Streamable HTTP).


+-------------------------------------------------------------------------+ | HTML5 / HTMX Frontend UI | | (URL Input + Live SSE Tool Execution Badges + Final Report) | +------------------------------------+------------------------------------+ | HTTP / Server-Sent Events (SSE) v +-------------------------------------------------------------------------+ | LangGraph Orchestrator (Python Backend) | | | | +----------------+ +-------------------+ +----------------+ | | | 1. Triage & | --> | 2. Content & | --> | 3. Fact-Check | | | | History Node | | Community Planner | | & Audit Loop | | | +----------------+ +-------------------+ +-------+--------+ | | | | | +----------------+ +-------------------+ | | | | 5. Final SSE | <-- | 4. Critic & | <-----------+ | | | Report Builder | | DB Commit Node | | | +----------------+ +-------------------+ | +--------+--------------------+--------------------+--------------------+ | MCP | MCP | MCP | MCP v (stdio/HTTP) v (stdio/HTTP) v (stdio/HTTP) v (stdio/HTTP) +----------------+ +----------------+ +----------------+ +----------------+ | youtube-mcp | | memory-db-mcp | | verify-soc-mcp | | sandbox-mcp | | (Python) | | (Go) | | (Go) | | (Go) | +--------+-------+ +--------+-------+ +--------+-------+ +--------+-------+ | | | | v v v v YouTube Data API ClickHouse DB Tavily / Reddit / Ephemeral Docker & Transcript API (Sidecar Volume) HN / Bluesky APIs (--network none)
### Hybrid Language Justification
* **Go (`golang`)** powers `memory-db-mcp`, `verify-social-mcp`, and `sandbox-mcp`. Go provides high-speed goroutine fan-out for querying multiple social APIs simultaneously, native TCP pooling for ClickHouse, and direct integration with the Docker Engine API.
* **Python** powers `youtube-mcp` and the **LangGraph Orchestrator**. Python offers first-class maintenance for `youtube-transcript-api` (handling YouTube caption changes) and native `langgraph` + `langchain-mcp-adapters` state orchestration.

---

## 3. Detailed MCP Server Specifications (Phase 1 Deliverables)

All MCP servers are built and tested independently using the MCP Inspector CLI prior to agent integration.

### 3.1 `youtube-mcp` (Python)
Provides on-demand inspection of the target YouTube video so the agent only pulls the data slices it needs.
* **`get_video_metadata(video_url: str)`**
 * **Action:** Extracts `video_id`, fetches channel ID, channel name, video title, upload date, chapter markers, full description text, and parses all outbound URLs (detecting `amzn.to`, `bit.ly`, UTM tags, and promo codes).
* **`get_transcript(video_id: str, start_sec: Optional[int], end_sec: Optional[int])`**
 * **Action:** Uses `youtube-transcript-api` to fetch manual or auto-generated transcripts (with automatic translation fallback to English), preserving exact `[MM:SS]` timestamps.
* **`get_comments(video_id: str, max_results: int = 100, sort_by: str = "top")`**
 * **Action:** Uses YouTube Data API v3 (`commentThreads.list`) to pull top-voted viewer comments and creator-pinned replies to surface crowdsourced fixes and sentiment.

### 3.2 `memory-db-mcp` (Go + ClickHouse v2 Driver)
Manages historical creator intelligence and stores immutable evaluation logs in ClickHouse.
* **`get_channel_history(channel_id: str)`**
 * **Action:** Runs a sub-millisecond OLAP aggregation returning total videos analyzed for this channel, rolling average accuracy score, percentage of videos with paid promotions, top recurring sponsors, and frequent collaborators.
* **`save_video_analysis(payload_json: str)`**
 * **Action:** Validates and inserts the completed video analysis record into the ClickHouse `video_analyses` table.
* **`query_historical_claims(topic_tag: str, limit: int = 10)`**
 * **Action:** Searches past JSON payloads in ClickHouse to check if the agent has already verified similar claims in previous videos.

### 3.3 `verify-social-mcp` (Go Concurrent Fan-Out)
Executes external web verification and cross-platform social sentiment mining using Go goroutines.
* **`search_web_factcheck(query: str, search_depth: str = "basic", domains: Optional[List[str]])`**
 * **Action:** Queries the **Tavily Search API** (1 credit for `basic`, 2 credits for `advanced`) to retrieve LLM-parsed web evidence. Supports site-scoping (e.g., `x.com` or `reddit.com`) when targeted domain checks are needed.
* **`search_social_reactions(topic_query: str, platforms: List[str])`**
 * **Action:** Launches parallel Go routines across requested open APIs and merges results:
   * **Reddit API:** OAuth2 client fetching top matching threads and highest-upvoted comments (100 req/min free tier).
   * **Hacker News (Algolia REST API):** Keyless search across technical stories and expert comment threads (`hn.algolia.com`).
   * **Bluesky (AT Protocol API):** Queries `app.bsky.feed.searchPosts` (3,000 req/5 min) for real-time public discourse.
   * **Stack Exchange API:** Queries technical Q&A threads for programming or scientific verification.

### 3.4 `sandbox-mcp` (Go + Docker SDK)
Allows the agent to verify mathematical claims, financial ROI pitches, or benchmark numbers deterministically.
* **`execute_python_audit(code: str, timeout_sec: int = 10)`**
 * **Action:** Uses the official Go Docker SDK to run the provided Python snippet inside a locked-down `python:3.12-slim` container.
 * **Security Constraints:** Enforces `--network none` (zero internet access), `--memory 256m`, `--cpus 0.5`, `--read-only` root filesystem, and automatic container destruction (`AutoRemove: true`) immediately after `stdout`/`stderr` capture.

---

## 4. OLAP Database Design (ClickHouse Sidecar)

ClickHouse runs as a persistent Docker container alongside the application. It uses `ReplacingMergeTree` to prevent duplicate entries if a video is re-analyzed while maintaining fast columnar scans across historical records.

### 4.1 Docker Compose Infrastructure (`docker-compose.yml`)
```yaml
services:
 clickhouse:
   image: clickhouse/clickhouse-server:latest
   container_name: youtube-agent-clickhouse
   ports:
     - "8123:8123" # HTTP interface
     - "9000:9000" # Native TCP interface (used by Go clickhouse-go/v2)
   environment:
     CLICKHOUSE_DB: youtube_agent
     CLICKHOUSE_USER: admin
     CLICKHOUSE_PASSWORD: secret
   volumes:
     - clickhouse_data:/var/lib/clickhouse
     - clickhouse_logs:/var/log/clickhouse-server
     - ./docker/clickhouse/init_schema.sql:/docker-entrypoint-initdb.d/init_schema.sql:ro
   restart: unless-stopped

volumes:
 clickhouse_data:
 clickhouse_logs:

4.2 ClickHouse Schema (docker/clickhouse/init_schema.sql)
CREATE DATABASE IF NOT EXISTS youtube_agent;

CREATE TABLE IF NOT EXISTS youtube_agent.video_analyses (
   channel_id LowCardinality(String),
   channel_name String,
   video_id String,
   video_title String,
   overall_accuracy_score UInt8,
   has_paid_promotion Bool,
   disclosed_properly Bool,
   sponsors Array(LowCardinality(String)),
   collaborators Array(String),
   topic_tags Array(LowCardinality(String)),
   analysis_payload JSON,
   analyzed_at DateTime64(3, 'UTC') DEFAULT now64()
)
ENGINE = ReplacingMergeTree(analyzed_at)
ORDER BY (channel_id, video_id);

4.3 analysis_payload JSON Contract
{
 "clickbait_buster": {
   "verdict": "Direct 1-sentence answer to the title.",
   "payoff_timestamp": "08:14",
   "clickbait_rating": "Medium"
 },
 "paid_promotion_details": {
   "affiliate_links_count": 5,
   "sponsor_segment_timestamps": ["02:10 - 03:45"],
   "bias_notes": "Host praises Product X battery life without noting the sponsored link."
 },
 "claims_verified": [
   {
     "timestamp": "04:32",
     "claim": "Host states Library X is 4x faster than Library Y.",
     "verdict": "Partially False",
     "evidence": "Official benchmarks show 1.4x speedup on average; 4x only occurs on synthetic cache hits.",
     "sources": ["https://...", "[https://news.ycombinator.com/](https://news.ycombinator.com/)..."]
   }
 ],
 "crowdsourced_additions": [
   {
     "category": "Tutorial Fix",
     "source": "YouTube Comment (@dev_user)",
     "detail": "At 11:20, flag --legacy-peer-deps is required on Node 22+."
   }
 ]
}

4.4 Core Analytical Query (get_channel_history)
SELECT
   count() AS total_videos_analyzed,
   round(avg(overall_accuracy_score), 1) AS avg_accuracy_score,
   countIf(has_paid_promotion = true) AS sponsored_videos_count,
   topK(5)(arrayJoin(sponsors)) AS frequent_sponsors,
   topK(5)(arrayJoin(collaborators)) AS frequent_collaborators,
   groupArray(10)(tuple(video_id, video_title, overall_accuracy_score, analyzed_at)) AS recent_runs
FROM youtube_agent.video_analyses FINAL
WHERE channel_id = @channel_id
GROUP BY channel_id;

5. Autonomous Agent Architecture (LangGraph Workflow)
The orchestrator uses a typed state graph rather than a single prompt, ensuring the agent can loop, self-correct weak search queries, and selectively call tools based on the video category.
5.1 LangGraph State Schema
from typing import TypedDict, List, Dict, Any, Optional

class AgentState(TypedDict):
   video_url: str
   video_id: str
   metadata: Dict[str, Any]          # Title, channel_id, links, chapters
   channel_history: Dict[str, Any]   # From ClickHouse OLAP
   transcript_summary: Dict[str, Any]# Clickbait buster, key takeaways, checklists
   extracted_claims: List[Dict]      # Claims needing verification + timestamps
   community_insights: List[Dict]    # YT comments + Reddit/HN/Bluesky findings
   verification_results: List[Dict]  # Audited claims from Tavily + Sandbox
   retry_count: int                  # Guardrail against infinite search loops (max 2)
   final_report_md: str              # Consumer-ready Markdown output

5.2 Node-by-Node Execution Flow
1. Node 1: Triage & History Loader (triage_node)
   * Calls youtube-mcp.get_video_metadata to inspect the title, description, outbound links, and channel ID.
   * Calls memory-db-mcp.get_channel_history to pull the creator's past accuracy and recurring sponsors from ClickHouse.
   * Determines the video domain (e.g., Tech Tutorial, Gadget Review, Finance/News, Health/Science) to guide downstream tool selection.
2. Node 2: Content & Community Miner (extraction_node)
   * Calls youtube-mcp.get_transcript and youtube-mcp.get_comments.
   * Formulates the Clickbait Buster (with timestamp), extracts the Actionable Checklist, flags Sponsorships/Affiliate Links, and distills Crowdsourced Additions from top viewer comments.
   * Emits a structured list of verifiable factual/numerical claims made by the host.
3. Node 3: Parallel Investigation & Audit Node (investigator_node)
   * For factual claims: Invokes verify-social-mcp.search_web_factcheck (Tavily basic search by default).
   * For community cross-checks: Invokes verify-social-mcp.search_social_reactions on the relevant platforms (e.g., Hacker News + Reddit for tech; Reddit + Bluesky for consumer products).
   * For quantitative/math claims: Generates a verification script and calls sandbox-mcp.execute_python_audit.
4. Node 4: Self-Reflection Critic (critic_node)
   * Evaluates whether every extracted claim has high-confidence evidence.
   * Conditional Edge: If evidence for a major claim is ambiguous and retry_count < 2, rewrites the search query, upgrades Tavily to advanced depth, and routes back to investigator_node. Otherwise, proceeds to Node 5.
5. Node 5: Memory Commit & Report Generator (commit_and_report_node)
   * Computes the video's overall_accuracy_score (0–100) based on the ratio of verified vs. debunked claims.
   * Calls memory-db-mcp.save_video_analysis to persist the evaluation into ClickHouse.
   * Streams the structured Markdown report to the frontend over Server-Sent Events (SSE).
6. Consumer Output Template
Every analyzed video produces a standardized, skimmable report:
# 🎯 Clickbait Buster
**TL;DW Verdict:** [Direct 1-2 sentence answer to the video's title/thumbnail]  
**Jump to the Point:** [08:14](https://youtube.com/watch?v=...&t=494s) | **Clickbait Rating:** Low / Medium / High

---

## 🛡️ Creator Context & Sponsorship Alert
* **Channel Track Record:** 86% Avg Accuracy across 14 analyzed videos (Trending ▲ +4%)
* **Detected Sponsors:** NordVPN ([02:10](...)) — *Recurring sponsor (appeared in 6 of last 10 videos)*
* **Affiliate Links Found:** 4 Amazon affiliate links in description
* **Bias Warning:** Host recommends Tool X without mentioning their active affiliate partnership; Reddit users report frequent crashes on version 4.2.

---

## ⚖️ Fact-Check Scorecard (Overall Accuracy: 80/100)
| Timestamp | Host's Claim | Verdict | Verification & Evidence |
| :--- | :--- | :--- | :--- |
| [04:32](...) | "Framework X reduces memory usage by 70%." | ⚠️ **Exaggerated** | Official docs & HN benchmarks show 25–35% reduction in production workloads. |
| [11:05](...) | "Staking yields 18% risk-free APY." | ❌ **Debunked** | Sandbox math & web audit confirm rewards are inflationary token emissions, not fixed yield. |

---

## 💡 Crowdsourced Additions (YouTube, Reddit & Hacker News)
* **🔧 Tutorial Fix (YouTube Top Comment):** Step 3 fails on Windows WSL2 unless you enable `systemd` in `/etc/wsl.conf` first.
* **💸 Free Alternative (Reddit r/selfhosted):** Commenters recommend open-source tool *Y* over the paid software pitched at [06:40](...).
* **⏳ 6-Month Reality Check:** Multiple users note the hinge loosens after heavy travel.

---

## 📋 Actionable Takeaways & Mentioned Resources
* [ ] Step 1: ...
* **Links & Tools Mentioned:** [Repo Link], [Book Title]

7. Phased Implementation Roadmap
Phase 1: Infrastructure & MCP Servers (First Priority)
* Step 1.1: Launch the persistent ClickHouse container via docker-compose.yml and verify init_schema.sql table creation.
* Step 1.2: Build memory-db-mcp in Go (clickhouse-go/v2 + mcp-go) and test get_channel_history and save_video_analysis via MCP Inspector.
* Step 1.3: Build verify-social-mcp in Go integrating Tavily, Reddit OAuth, Hacker News Algolia, and Bluesky AT Protocol with concurrent goroutines.
* Step 1.4: Build sandbox-mcp in Go using the Docker SDK to execute isolated Python verification scripts (--network none).
* Step 1.5: Build youtube-mcp in Python wrapping youtube-transcript-api and YouTube Data API v3 (metadata, transcript, comments).
Phase 2: Core LangGraph Agent
* Step 2.1: Connect all four MCP servers to the Python LangGraph orchestrator using langchain-mcp-adapters.
* Step 2.2: Implement the 5-node state graph (triage, extraction, investigator, critic, commit_and_report) and test end-to-end via CLI with sample YouTube URLs.
Phase 3: Frontend & Streaming API
* Step 3.1: Expose the LangGraph runner via FastAPI with a Server-Sent Events (SSE) endpoint (/api/analyze/stream).
* Step 3.2: Build a responsive HTML/HTMX frontend displaying live MCP tool status badges and rendering the final Markdown report.