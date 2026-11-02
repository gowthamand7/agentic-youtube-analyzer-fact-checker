# Required Configuration & API Credentials

This document lists everything required from your end to run the platform and its live test harness. You can add these keys to a `.env` file in the root of this project.

---

## 1. Environment Keys Checklist (`.env`)

Create a `.env` file in the project root with the following keys:

```dotenv
# ==========================================
# 1. CORE LLM & ORCHESTRATION (REQUIRED)
# ==========================================
# Anthropic API Key for the Claude Haiku orchestrator
ANTHROPIC_API_KEY=your_anthropic_api_key_here
ANTHROPIC_MODEL=claude-haiku-5-5

# ==========================================
# 2. FACT-CHECKING SEARCH ENGINE (REQUIRED)
# ==========================================
# Tavily Search API key for web fact-checking (get free tier at https://tavily.com)
TAVILY_API_KEY=tvly-your_tavily_key_here

# ==========================================
# 3. YOUTUBE INGESTION (HIGHLY RECOMMENDED)
# ==========================================
# Google Cloud YouTube Data API v3 key for fetching top comments & creator pinned threads
# (Transcripts are fetched directly, but comments require or benefit from this key)
YOUTUBE_API_KEY=your_youtube_api_v3_key_here

# ==========================================
# 4. SOCIAL AUDITING (OPTIONAL / AUTO-FALLBACK)
# ==========================================
# Hacker News (Algolia) & Bluesky (AT Protocol) are 100% KEYLESS and work out of the box!
# Reddit API credentials (Optional: if omitted, system falls back to keyless search):
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=AgenticYouTubeAnalyzer/1.0

# ==========================================
# 5. CLICKHOUSE DATABASE (DEFAULT LOCAL CONFIG)
# ==========================================
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=9000
CLICKHOUSE_HTTP_PORT=8123
CLICKHOUSE_DB=youtube_agent
CLICKHOUSE_USER=admin
CLICKHOUSE_PASSWORD=secret
```

---

## 2. Local Host Services to Start

### 🐳 Docker Desktop
- **Action required:** Please launch **Docker Desktop** on Windows and ensure the engine status shows **Running** (green).
- **Why it's needed:**
  1. Powers the persistent **ClickHouse** sidecar container (`youtube-agent-clickhouse`) for channel history and creator accuracy memory.
  2. Powers the isolated **Python Code Sandbox** (`sandbox-mcp`) for deterministic math and APY verification with zero-network isolation (`--network none`).

---

## 3. Quick Verification
Once you have created `.env` and started Docker Desktop, the Phase 0 harness (`python scripts/harness_check.py`) will automatically test all connections and confirm ready status.


---

## 4. Running the Platform

```powershell
# one-time
uv sync
go build -o bin/memory-db-mcp.exe ./mcp-servers/memory-db
go build -o bin/sandbox-mcp.exe ./mcp-servers/sandbox
go build -o bin/verify-social-mcp.exe ./mcp-servers/verify-social
docker compose up -d

# verify
uv run python scripts/harness_check.py          # env, Docker, ClickHouse, Tavily
uv run python scripts/mcp_protocol_check.py     # all 4 MCP servers over stdio

# analyze one video from the CLI
uv run python scripts/run_e2e_test.py --url "https://www.youtube.com/watch?v=..."

# web UI + SSE API at http://localhost:8000
uv run uvicorn app.main:app --port 8000

# tests
uv run pytest
go test ./mcp-servers/memory-db/... ./mcp-servers/sandbox/... ./mcp-servers/verify-social/...
```

Notes:
- Reddit uses the official API (`REDDIT_CLIENT_ID`/`SECRET`, app-only OAuth). Without valid credentials the Reddit platform returns an error entry and the other platforms still work. Set `REDDIT_TAVILY_FALLBACK=1` to instead search Reddit via Tavily (`include_domains=reddit.com`, 1 credit per lookup).
- YouTube may rate-limit transcript requests from your IP after repeated runs; the analysis then aborts with a clear message.

---

## 5. Debugging a Request

Every analysis (UI, API, or CLI) writes a JSONL log to `logs/<timestamp>_<request-id>.jsonl` (git-ignored) and prints one console line per event prefixed with `[request-id]`. The UI shows the request ID next to the step count.

```powershell
uv run python scripts/show_log.py            # timeline of the latest request
uv run python scripts/show_log.py a1b2c3d4   # a specific request ID
uv run python scripts/show_log.py latest -v  # also tool args/results and LLM outputs
```

Logged: tool calls (arguments, truncated result, duration, traceback on failure), LLM calls (purpose, prompt size, structured output, duration), each model tool-call round, verdicts, node transitions, and the final status (`ok`, `aborted`, `request_error`, `request_cancelled` for a closed browser tab).

Env: `LOG_DIR` changes the directory; `LOG_PROMPTS=1` also records the full LLM prompts (large, may include transcript text).

---

## 6. Transcript & Metadata Cache

`youtube-mcp` caches YouTube lookups on disk in `cache/` (git-ignored; override with `CACHE_DIR`), so re-analyzing a video does not contact YouTube again. The activity timeline shows `(cached)` on cache hits.

| Data | Cached for | Notes |
| :--- | :--- | :--- |
| Transcript (found) | 30 days | Full transcript stored once; `start_sec`/`end_sec` slice it afterwards |
| Transcript (disabled/none) | 24 hours | |
| Video metadata | 6 hours | View counts and descriptions drift |
| Comments | not cached | Official API, cheap |
| Blocks / outages / unknown errors | never | A temporary YouTube rate limit is never remembered |

To force a fresh fetch, delete the matching file in `cache/` (for example `cache/transcript_<videoId>_en.json`) or the whole folder.

---

## 7. Saved Reports & the Developer Trace

**Saved reports.** A finished analysis is stored in `reports/<videoId>.json` (git-ignored; override with `REPORT_DIR`). Analyzing a video you have already checked shows the saved report instantly (any URL form: `watch?v=`, `youtu.be`, `shorts`) and does not run the agent or spend any API credits. The banner offers **Re-run analysis**, which runs a fresh analysis and replaces the saved report. Aborted runs (no transcript, blocked, etc.) are never saved.

API: `GET /api/analyze/stream?video_url=...&refresh=true` and `POST /api/analyze {"video_url": "...", "refresh": true}` force a re-run; omit `refresh` to reuse. The JSON response includes `cached` and `generated_at`.

**Developer trace.** In the "Agent activity" panel, the **Developer trace** tab streams the raw run line by line, with colour-coded entries:

```
  2.85s  → CALL  search_web_factcheck(query="Library X vs Y benchmark", search_depth="basic")
  3.56s  ← RETURN  search_web_factcheck (710ms)  5 sources: docs.x ...        (click to expand arguments / result)
  3.56s  → LLM   tool-use round 1  model=claude-haiku-5-5  messages=4
  3.57s  ← LLM   round 1 decided: ...                                         (expand: the model's stated reasoning)
```

It shows every tool call with parameter names, results (truncated at 3000 characters), durations, errors with tracebacks, each model call (model, input size, structured output), verdicts and node transitions. **Copy trace** copies it as text. The same records are written to `logs/` (see section 5).

---

## 8. Report v2 and "I already know this"

**What the report contains.** A summary-first report chosen per video: TL;DW and at-a-glance chips; **What you'll learn** (key ideas with timestamps, each badged *checked*, *disputed*, *contradicted* or *unchecked*); **What's wrong or doubtful** (only contradicted, exaggerated or partly false claims, with quoted evidence and sources, plus a collapsed "couldn't verify" list); **From the comments** (only comments that add information about the topic); **Elsewhere on the web**; dynamic sections the model picks for the video (for example *Steps to follow* for a tutorial or *Exam numbers to know*); every checked claim in a collapsed list; and *Before you watch* (sponsors, affiliate links, bias). The old single "accuracy score" is replaced by "N of M checked claims hold up", weighted by claim importance. The number of claims scales with video length (3 to 8).

**Personalization.** Click **I already know this** on a key idea or comment. It is saved to `profile/knowledge.json` (git-ignored; override with `KNOWLEDGE_FILE`) and hidden from future reports; a collapsed "You already know N of these points" group keeps it recoverable. Matching uses the idea's short concept name plus one small model call that only hides an item when a known entry states the *same* knowledge (related-but-different ideas stay visible). Use **Undo** right after marking, or **Your knowledge** (top right) to remove entries.

API: `GET /api/knowledge`, `POST /api/knowledge {"video_id", "item_id"}` (the text is taken from the saved report, never from the client), `DELETE /api/knowledge/{id}`.

Reports saved before v2 still open in the old layout; press **Re-run analysis** to upgrade one. See `docs/ROADMAP.md` for the held hosted-API and Chrome-extension work.
