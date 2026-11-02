# Design Document: Agentic YouTube Analyzer & Fact-Checking Platform

**Date:** 2026-11-01  
**Status:** In Review / Ready for Builder Check  
**Approach Selected:** Approach 1 (Modular Bottom-Up MCP Harness First)  
**Primary Model:** Anthropic Claude 3.5 Haiku (configurable via `ANTHROPIC_MODEL`)  
**Execution Environment:** Windows (Go 1.25.2, Python 3.14 / `uv`, Docker Desktop)

---

## 1. Intent & The "Why"
Traditional video summarizers simply compress a video's transcript, repeating clickbait headlines, propagating factual errors, and ignoring undisclosed sponsorships. 

This platform transforms video consumption into an **autonomous investigative audit**. Given only a YouTube URL, an intelligent agent audits factual claims against live web evidence, cross-checks community corrections across social platforms (YouTube comments, Reddit, Hacker News, Bluesky), verifies quantitative calculations in an isolated Docker sandbox, and tracks creator trustworthiness over time in an OLAP database (ClickHouse).

---

## 2. Goals, Non-Goals & Anti-Goals

### 2.1 Goals
- **Clickbait Buster:** Extract and state the core answer to the video title/thumbnail in the very first sentence with the exact timestamp payoff.
- **Sponsorship & Bias Disclosure:** Parse outbound links, affiliate tags, and historical sponsor frequency to flag undeclared sponsorships.
- **Crowdsourced Corrections:** Mine YouTube top comments, Reddit, Hacker News, and Bluesky for actionable tutorial fixes, product durability warnings, or cheaper/free alternatives.
- **Fact-Checking & Deterministic Audits:** Verify statistical and factual claims via live Tavily search and run math/benchmark validations inside a `--network none` Docker Python container.
- **Historical Memory:** Persist video evaluations in ClickHouse using `ReplacingMergeTree` to maintain rolling accuracy scores and sponsor frequencies per creator.
- **Multi-Language Ingestion & English Output:** Accept videos, transcripts, and comments in any spoken language. The agent understands multi-lingual context and translates/synthesizes all claims into an English fact-checking report.
- **Strict Transcript Requirement (Fail-Fast):** If transcripts/captions are disabled or missing, the system aborts early with a clean explanatory error notice rather than hallucinating spoken claims.
- **Development Test Harness First:** Provide a comprehensive verification harness to test all external live services, Docker connectivity, and MCP JSON-RPC protocols independently before agent integration.
- **Live Streaming UI:** Stream agent progress, tool execution badges, and the final Markdown report over Server-Sent Events (SSE) to an HTML5/HTMX interface.

### 2.2 Non-Goals
- Real-time video frame-by-frame computer vision or audio tone parsing (the system operates on captions, transcripts, and metadata).
- Fully automated social media posting or commenting on behalf of the user.
- Multi-user authentication, billing, or multi-tenant database partitioning in Phase 1.

### 2.3 Anti-Goals
- **No Hallucinated Evidence:** The agent must never produce a fact-check verdict without citing retrieved web or community sources with links/timestamps.
- **No Fragile Megaprompting:** Avoid a single massive monolithic LLM call; use a stateful LangGraph with typed state and a reflection loop.
- **No Uncontrolled API Burn:** Enforce maximum retry limits (max 2 search retries) in the self-reflection critic node.

---

## 3. Approaches Considered & Selected Approach

### Approaches Considered:
1. **Approach 1: Modular Bottom-Up MCP Harness (Selected)**  
   Build and verify each live external integration and MCP server via a dedicated test harness before wiring up the LangGraph agent. Ensures zero compound debugging errors.
2. **Approach 2: Agent-First with Inline Tools**  
   Wire LangGraph directly to inline Python functions, refactoring to Go/Python MCP servers later. Rejected because it incurs major architectural rework.
3. **Approach 3: All-Python Unified Monolith**  
   Implement all tools in Python. Rejected because it abandons Go's high-speed goroutine fan-out for multi-platform social search and native ClickHouse TCP pooling.

**Decision:** The user selected **Approach 1**.

---

## 4. System Architecture & Detailed Component Design

### 4.1 Hybrid Technology Stack & Transport Lifecycle
- **Go (`golang`)**: Powers `memory-db-mcp`, `verify-social-mcp`, and `sandbox-mcp`.
- **Python (`uv`)**: Powers `youtube-mcp`, the **LangGraph Orchestrator** (Claude 3.5 Haiku), and the FastAPI SSE streaming backend.
- **MCP Transport**: Standard `stdio` subprocess execution managed via `langchain-mcp-adapters`. Go binaries are pre-compiled into a local `bin/` directory and executed directly as child processes with stdin/stdout JSON-RPC communication.
- **ClickHouse**: Columnar OLAP sidecar running via `docker-compose.yml`.
- **Frontend**: Lightweight HTML5 + HTMX with Tailwind CSS for zero-build UI streaming.

---

### 4.2 Phase 0: Development & Live API Harness (`harness/`)
Before building agent logic, an automated harness guarantees all live dependencies work:
1. `harness_check.py`:
   - Checks presence and validity of required environment keys (`ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, `YOUTUBE_API_KEY` / transcript capability, Reddit/Bluesky credentials if configured).
   - Verifies Docker daemon status and ClickHouse TCP port (`9000`) / HTTP port (`8123`) accessibility.
   - Pings Tavily search API with a basic query.
2. **MCP Protocol Test Suite:**
   - Programmatically launches each MCP server subprocess over `stdio`.
   - Sends standard JSON-RPC `tools/list` and `tools/call` payloads and asserts conforming responses.

---

### 4.3 Phase 1: Model Context Protocol (MCP) Servers

#### Server 1: `memory-db-mcp` (Go)
- **Path:** `mcp-servers/memory-db/`
- **Dependencies:** `github.com/ClickHouse/clickhouse-go/v2`, `github.com/mark3labs/mcp-go`
- **Tools:**
  - `get_channel_history(channel_id: string)`: Aggregates historical accuracy score, sponsored video count, frequent sponsors, and previous run records.
  - `save_video_analysis(payload_json: string)`: Inserts evaluated analysis into `youtube_agent.video_analyses`.
  - `query_historical_claims(topic_tag: string, limit: int)`: Checks for previously verified claims matching the topic tag, allowing the agent to reuse prior fact-checks and save Tavily search credits.

#### Server 2: `sandbox-mcp` (Go)
- **Path:** `mcp-servers/sandbox/`
- **Dependencies:** `github.com/docker/docker/client`, `github.com/mark3labs/mcp-go`
- **Tools:**
  - `execute_python_audit(code: string, timeout_sec: int)`: Executes Python calculation snippets in an isolated `python:3.12-slim` container with:
    - `--network none`
    - `--memory 256m`
    - `--cpus 0.5`
    - `--read-only` root filesystem
    - Automatic container cleanup (`AutoRemove: true`)

#### Server 3: `verify-social-mcp` (Go)
- **Path:** `mcp-servers/verify-social/`
- **Dependencies:** `github.com/mark3labs/mcp-go`
- **Tools:**
  - `search_web_factcheck(query: string, search_depth: string, domains: []string)`: Queries Tavily Search API.
  - `search_social_reactions(topic_query: string, platforms: []string)`: Concurrent fan-out via goroutines to:
    - **Hacker News:** Algolia REST API (`hn.algolia.com/api/v1/search`) - Keyless
    - **Bluesky:** AT Protocol public API (`public.api.bsky.app`) - Keyless
    - **Reddit:** Reddit OAuth if configured, falling back to public Reddit JSON search if unconfigured
    - **Stack Exchange:** Public search API for developer questions

#### Server 4: `youtube-mcp` (Python)
- **Path:** `mcp-servers/youtube/`
- **Dependencies:** `mcp`, `youtube-transcript-api`, `google-api-python-client`
- **Tools:**
  - `get_video_metadata(video_url: str)`: Extracts video ID, title, channel ID, description, outbound links, and chapters.
  - `get_transcript(video_id: str, languages: Optional[List[str]] = None)`: Fetches timestamped transcript segments in any available language. If transcripts are disabled or unavailable, returns an explicit `TRANSCRIPTS_DISABLED` status code allowing `triage_node` / `extraction_node` to abort early with an explanatory message.
  - `get_comments(video_id: str, max_results: int = 100, sort_by: str = "top")`: Retrieves top comments and creator replies in any language.

---

### 4.4 Phase 2: LangGraph Orchestrator (Python)
- **Model:** Claude 3.5 Haiku (`claude-3-5-haiku-20241022` or latest alias) via `@langchain/anthropic`.
- **Language Policy:** Ingests transcripts and comments in any language; outputs all verdicts, quotes, and reports in English.
- **Graph State:**
  ```python
  class AgentState(TypedDict):
      video_url: str
      video_id: str
      metadata: Dict[str, Any]
      channel_history: Dict[str, Any]
      transcript_summary: Dict[str, Any]
      extracted_claims: List[Dict[str, Any]]
      community_insights: List[Dict[str, Any]]
      verification_results: List[Dict[str, Any]]
      retry_count: int
      error_message: Optional[str]
      final_report_md: str
  ```
- **Graph Nodes & Execution Mechanics:**
  1. `triage_node`: Fetches metadata via `youtube-mcp` and creator history via `memory-db-mcp`.
  2. `extraction_node`: Fetches transcript and comments. If transcript is disabled, sets `error_message="Transcripts are disabled or unavailable for this video; cannot perform spoken claim auditing."` and routes directly to report generation for clean early exit. Otherwise, formulates the Clickbait Buster in English, extracts 3–5 verifiable claims with timestamps, flags sponsor links, and extracts tutorial fixes.
  3. `investigator_node`: Operates as an autonomous tool-calling node where Claude 3.5 Haiku is dynamically bound to `verify-social-mcp` (Tavily + social platforms), `sandbox-mcp` (Docker math audit), and `memory-db-mcp.query_historical_claims` to gather evidence.
  4. `critic_node`: Evaluates evidence sufficiency. If evidence is ambiguous and `retry_count < 2`, modifies search queries, upgrades search depth, and loops back to `investigator_node`. Otherwise proceeds.
  5. `commit_and_report_node`: Computes overall score (0–100), stores record in ClickHouse via `memory-db-mcp` (skipped if aborted due to missing transcript), and renders the standardized Markdown report in English.

---

### 4.5 Phase 3: Streaming API & HTMX Frontend
- **FastAPI Backend (`app/main.py`):**
  - Endpoint `GET /api/analyze/stream?video_url=...`: Primary Server-Sent Events (SSE) streaming endpoint consumed by HTMX (`hx-ext="sse"`), streaming live node events, tool execution badges, and the final Markdown report directly on connection.
  - Endpoint `POST /api/analyze`: Synchronous/CLI endpoint returning completed JSON report payload.
- **Frontend (`app/static/index.html`):**
  - Clean HTML5 + HTMX interface.
  - Real-time step progress indicator and interactive accordion for fact-check cards.

---

## 5. What's Left to the Builder
- Exact directory layout details and package naming conventions.
- Prompt phrasing and few-shot rubrics for Claude 3.5 Haiku to maximize claim extraction accuracy and critical evaluation.
- Fallback strategies for when optional social APIs (e.g. Reddit OAuth credentials) are not populated (graceful degradation to available platforms like Hacker News and Bluesky).
- Formatting nuances in the SSE streaming payload.
