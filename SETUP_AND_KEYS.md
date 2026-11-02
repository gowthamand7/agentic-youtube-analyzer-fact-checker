# Required Configuration & API Credentials

This document lists everything required from your end to run the platform and its live test harness. You can add these keys to a `.env` file in the root of this project.

---

## 1. Environment Keys Checklist (`.env`)

Create a `.env` file in the project root with the following keys:

```dotenv
# ==========================================
# 1. CORE LLM & ORCHESTRATION (REQUIRED)
# ==========================================
# Anthropic API Key for Claude 3.5 Haiku orchestrator
ANTHROPIC_API_KEY=your_anthropic_api_key_here
ANTHROPIC_MODEL=claude-3-5-haiku-20241022

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
