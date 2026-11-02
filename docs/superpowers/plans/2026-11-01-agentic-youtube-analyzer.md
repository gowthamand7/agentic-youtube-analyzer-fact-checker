# Agentic YouTube Analyzer & Fact-Checking Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an autonomous investigative YouTube analyzer and fact-checking platform that audits video claims, flags undisclosed sponsorships, and mines crowdsourced fixes using a hybrid Go/Python MCP architecture, ClickHouse OLAP storage, and a Claude 3.5 Haiku LangGraph orchestrator.

**Architecture:** A Python LangGraph agent orchestrates four standalone Model Context Protocol (MCP) servers communicating over `stdio` JSON-RPC: three high-performance Go servers (`memory-db-mcp`, `verify-social-mcp`, `sandbox-mcp`) and one Python server (`youtube-mcp`). Video evaluations persist in a ClickHouse OLAP container, while a FastAPI backend streams real-time tool events and Markdown reports over SSE to an HTML5/HTMX frontend.

**Tech Stack:** Go 1.25 (mcp-go, clickhouse-go/v2, Docker SDK), Python 3.14 / `uv` (langgraph, langchain-anthropic, langchain-mcp-adapters, fastapi, youtube-transcript-api), ClickHouse, Docker, HTMX.

**Spec:** `docs/superpowers/specs/2026-11-01-agentic-youtube-analyzer-design.md`

## Global Constraints
- Target platform: Windows 11 host with Go 1.25, Python 3.14 / `uv`, and Docker Desktop.
- MCP transport: `stdio` JSON-RPC with Go executables compiled into `bin/`.
- Orchestrator LLM: Anthropic Claude 3.5 Haiku (`claude-3-5-haiku-20241022` or latest alias) configured via `ANTHROPIC_API_KEY`.
- Language ingestion: Accept transcripts and comments in any language; generate all outputs, verdicts, and reports in English.
- Missing transcript policy: Fail fast with an explanatory error notice (`"Transcripts are disabled or unavailable for this video; cannot perform spoken claim auditing"`) and abort before ClickHouse commit.
- Docker sandbox security: Run Python math snippets in `python:3.12-slim` with `--network none`, `--memory 256m`, `--cpus 0.5`, `--read-only`, and `AutoRemove: true`.

## Review Focus
- YouTube video with disabled or unavailable captions -> Aborts gracefully without crashing or committing bogus records.
- ClickHouse container unreachable or down -> Clear actionable error message from `harness_check.py` or MCP server.
- Tavily search returns empty or ambiguous evidence -> Critic node loops at most 2 times before producing best-effort scorecard without infinite loop.
- Host makes claims with no citations -> Verdict is marked "Unverified" or "Lacks Evidence" with clear explanation.
- Docker daemon not running during sandbox execution -> Clean error reporting from `sandbox-mcp` without unhandled Go panic.

---

### Task 1: Environment & Live Verification Harness (Phase 0)

**Files:**
- Create: `docker-compose.yml`
- Create: `docker/clickhouse/init_schema.sql`
- Create: `scripts/harness_check.py`
- Test: `tests/test_harness_check.py`

**Interfaces:**
- Produces: `scripts/harness_check.py` executable returning exit code `0` when environment, keys, and services are healthy.

- [ ] **Step 1: Write `docker-compose.yml` and `docker/clickhouse/init_schema.sql`**
  - Define `clickhouse` service running `clickhouse/clickhouse-server:latest` exposing ports `8123` (HTTP) and `9000` (Native TCP), with volume mounting for `docker/clickhouse/init_schema.sql`.
  - Schema defines database `youtube_agent` and table `video_analyses` using `ReplacingMergeTree(analyzed_at)` ordered by `(channel_id, video_id)`.

- [ ] **Step 2: Write test for `scripts/harness_check.py`**
  - Assert that `harness_check.py` verifies required environment variables (`ANTHROPIC_API_KEY`, `TAVILY_API_KEY`), checks Docker daemon status, checks ClickHouse TCP connectivity, and tests Tavily basic search.

- [ ] **Step 3: Implement `scripts/harness_check.py`**
  - Implement health checks for environment variables, Python dependencies, Go toolchain, Docker daemon, ClickHouse ping, and live Tavily API ping.

- [ ] **Step 4: Run harness check and verify output**
  - Run: `python scripts/harness_check.py`
  - Expected: Clear summary report of available live services and any missing credentials.

- [ ] **Step 5: Commit**
  - Commit Phase 0 harness and ClickHouse infrastructure.

---

### Task 2: `memory-db-mcp` (Go + ClickHouse)

**Files:**
- Create: `mcp-servers/memory-db/go.mod`
- Create: `mcp-servers/memory-db/main.go`
- Create: `mcp-servers/memory-db/db.go`
- Test: `mcp-servers/memory-db/main_test.go`

**Interfaces:**
- Produces: Go binary `bin/memory-db-mcp.exe` implementing MCP tools:
  - `get_channel_history(channel_id: string) -> JSON`
  - `save_video_analysis(payload_json: string) -> JSON`
  - `query_historical_claims(topic_tag: string, limit: int) -> JSON`

- [ ] **Step 1: Write failing unit test for `memory-db-mcp` tools**
  - Write `main_test.go` verifying tool registration, JSON schema generation for the 3 tools, and query builders.

- [ ] **Step 2: Run test to verify it fails**
  - Run: `go test ./mcp-servers/memory-db/... -v`
  - Expected: FAIL

- [ ] **Step 3: Implement `memory-db-mcp` using `mcp-go` and `clickhouse-go/v2`**
  - Implement ClickHouse connection pool over native TCP (`localhost:9000`).
  - Implement `get_channel_history` using `SELECT count(), round(avg(overall_accuracy_score), 1), ... FROM youtube_agent.video_analyses FINAL WHERE channel_id = ?`.
  - Implement `save_video_analysis` inserting row with JSON payload.
  - Implement `query_historical_claims` querying past verified claims by tag.

- [ ] **Step 4: Compile and test binary execution**
  - Run: `go build -o bin/memory-db-mcp.exe ./mcp-servers/memory-db`
  - Run: `go test ./mcp-servers/memory-db/... -v`
  - Expected: PASS

- [ ] **Step 5: Commit**
  - Commit `memory-db-mcp` implementation.

---

### Task 3: `sandbox-mcp` (Go + Docker SDK)

**Files:**
- Create: `mcp-servers/sandbox/go.mod`
- Create: `mcp-servers/sandbox/main.go`
- Create: `mcp-servers/sandbox/runner.go`
- Test: `mcp-servers/sandbox/runner_test.go`

**Interfaces:**
- Produces: Go binary `bin/sandbox-mcp.exe` implementing MCP tool:
  - `execute_python_audit(code: string, timeout_sec: int) -> JSON`

- [ ] **Step 1: Write failing unit test for Python sandbox execution**
  - Write `runner_test.go` asserting container creation config with `--network none`, `--memory 268435456`, `--read-only`, and verifying output capture of `print(123 * 456)`.

- [ ] **Step 2: Run test to verify it fails**
  - Run: `go test ./mcp-servers/sandbox/... -v`
  - Expected: FAIL

- [ ] **Step 3: Implement `sandbox-mcp` with Docker Engine SDK**
  - Connect to Docker daemon via `client.NewClientWithOpts(client.FromEnv)`.
  - Implement container execution running `python:3.12-slim -c <code>` with safety limits (10s timeout, no network, memory cap).
  - Register tool `execute_python_audit` with `mcp-go`.

- [ ] **Step 4: Compile and test binary**
  - Run: `go build -o bin/sandbox-mcp.exe ./mcp-servers/sandbox`
  - Run: `go test ./mcp-servers/sandbox/... -v`
  - Expected: PASS

- [ ] **Step 5: Commit**
  - Commit `sandbox-mcp` implementation.

---

### Task 4: `verify-social-mcp` (Go Concurrent Fan-Out)

**Files:**
- Create: `mcp-servers/verify-social/go.mod`
- Create: `mcp-servers/verify-social/main.go`
- Create: `mcp-servers/verify-social/tavily.go`
- Create: `mcp-servers/verify-social/social.go`
- Test: `mcp-servers/verify-social/main_test.go`

**Interfaces:**
- Produces: Go binary `bin/verify-social-mcp.exe` implementing MCP tools:
  - `search_web_factcheck(query: string, search_depth: string, domains: []string) -> JSON`
  - `search_social_reactions(topic_query: string, platforms: []string) -> JSON`

- [ ] **Step 1: Write failing test for Tavily and social search handlers**
  - Write `main_test.go` verifying Tavily API request formulation and concurrent goroutine fan-out merging Hacker News, Bluesky, and Reddit results into a unified struct.

- [ ] **Step 2: Run test to verify it fails**
  - Run: `go test ./mcp-servers/verify-social/... -v`
  - Expected: FAIL

- [ ] **Step 3: Implement `verify-social-mcp`**
  - Implement `search_web_factcheck` querying Tavily REST API (`https://api.tavily.com/search`).
  - Implement `search_social_reactions` using `sync.WaitGroup` to concurrently query Hacker News Algolia API, Bluesky search API, and Reddit public search.
  - Register tools via `mcp-go` stdio server.

- [ ] **Step 4: Compile and test binary**
  - Run: `go build -o bin/verify-social-mcp.exe ./mcp-servers/verify-social`
  - Run: `go test ./mcp-servers/verify-social/... -v`
  - Expected: PASS

- [ ] **Step 5: Commit**
  - Commit `verify-social-mcp` implementation.

---

### Task 5: `youtube-mcp` (Python MCP Server)

**Files:**
- Create: `mcp-servers/youtube/pyproject.toml`
- Create: `mcp-servers/youtube/server.py`
- Create: `mcp-servers/youtube/extractor.py`
- Test: `tests/test_youtube_mcp.py`

**Interfaces:**
- Produces: Python MCP server `mcp-servers/youtube/server.py` implementing MCP tools:
  - `get_video_metadata(video_url: str) -> Dict`
  - `get_transcript(video_id: str, languages: Optional[List[str]]) -> Dict`
  - `get_comments(video_id: str, max_results: int) -> Dict`

- [ ] **Step 1: Write failing test for YouTube tools**
  - Assert URL parsing, metadata extraction, transcript fetching with timestamp preservation, and fail-fast handling for disabled transcripts (`TRANSCRIPTS_DISABLED`).

- [ ] **Step 2: Run test to verify it fails**
  - Run: `pytest tests/test_youtube_mcp.py -v`
  - Expected: FAIL

- [ ] **Step 3: Implement `extractor.py` and `server.py`**
  - Wrap `youtube-transcript-api` with language fallback and `TranscriptsDisabled` exception catching.
  - Implement metadata extractor (parsing title, author, chapters, outbound URLs, affiliate tags).
  - Implement comments extractor via YouTube Data API v3 (or fallback scraper).
  - Expose tools via official Python `mcp` SDK over `stdio`.

- [ ] **Step 4: Run test to verify it passes**
  - Run: `pytest tests/test_youtube_mcp.py -v`
  - Expected: PASS

- [ ] **Step 5: Commit**
  - Commit `youtube-mcp` implementation.

---

### Task 6: LangGraph Orchestrator (Claude 3.5 Haiku Agent)

**Files:**
- Create: `agent/state.py`
- Create: `agent/prompts.py`
- Create: `agent/mcp_client.py`
- Create: `agent/nodes.py`
- Create: `agent/graph.py`
- Test: `tests/test_agent_graph.py`

**Interfaces:**
- Consumes: MCP tools from Tasks 2, 3, 4, 5 via `langchain-mcp-adapters`.
- Produces: `run_agent(video_url: str) -> AsyncGenerator[Dict[str, Any], None]` yielding streaming state updates and final report.

- [ ] **Step 1: Write failing test for state graph execution flow**
  - Test state initialization, missing transcript short-circuiting, and node transitions with mocked tool responses.

- [ ] **Step 2: Run test to verify it fails**
  - Run: `pytest tests/test_agent_graph.py -v`
  - Expected: FAIL

- [ ] **Step 3: Implement state schema, prompts, and nodes**
  - Implement `AgentState` in `agent/state.py`.
  - Implement Claude 3.5 Haiku prompts in `agent/prompts.py` with few-shot rubrics for claim extraction, Clickbait Buster, and verification scorecard.
  - Implement `mcp_client.py` initializing stdio clients for the 4 MCP servers.
  - Implement the 5 nodes: `triage_node`, `extraction_node`, `investigator_node` (ReAct tool calling), `critic_node` (reflection loop with `retry_count < 2`), and `commit_and_report_node`.
  - Wire up `StateGraph(AgentState)` with conditional routing.

- [ ] **Step 4: Run graph unit tests**
  - Run: `pytest tests/test_agent_graph.py -v`
  - Expected: PASS

- [ ] **Step 5: Commit**
  - Commit LangGraph orchestrator implementation.

---

### Task 7: FastAPI Streaming API & HTMX Frontend

**Files:**
- Create: `app/main.py`
- Create: `app/static/index.html`
- Create: `app/static/styles.css`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `agent/graph.py`
- Produces:
  - `GET /api/analyze/stream?video_url=...`: Server-Sent Events stream.
  - `POST /api/analyze`: JSON payload endpoint.
  - Web UI at `http://localhost:8000/`.

- [ ] **Step 1: Write failing API test**
  - Test SSE streaming response headers (`text/event-stream`) and event framing.

- [ ] **Step 2: Run test to verify it fails**
  - Run: `pytest tests/test_api.py -v`
  - Expected: FAIL

- [ ] **Step 3: Implement FastAPI app and HTMX interface**
  - Implement FastAPI endpoints in `app/main.py`.
  - Build `index.html` using Tailwind CSS and HTMX with `hx-ext="sse"` to stream real-time tool badges (`triage`, `extraction`, `investigation`, `critic`, `report`) and render the final Markdown scorecard.

- [ ] **Step 4: Run test to verify it passes**
  - Run: `pytest tests/test_api.py -v`
  - Expected: PASS

- [ ] **Step 5: Commit**
  - Commit FastAPI server and HTMX frontend.

---

### Task 8: End-to-End Live Verification & Smoke Test

**Files:**
- Create: `scripts/run_e2e_test.py`

- [ ] **Step 1: Run complete `harness_check.py` on the live environment**
  - Verify all 4 MCP binaries compile and respond over stdio.
  - Verify ClickHouse container is active and accepting connections.

- [ ] **Step 2: Execute end-to-end analysis on a sample live YouTube video**
  - Run: `python scripts/run_e2e_test.py --url "<sample_youtube_url>"`
  - Verify Clickbait Buster verdict, timestamp payoff, extracted claims, and ClickHouse record insertion.

- [ ] **Step 3: Verify missing transcript handling**
  - Run end-to-end test against a video with disabled captions and verify clean abort message without server crash.

- [ ] **Step 4: Commit final verification and documentation updates**
  - Commit final integration updates and documentation.
