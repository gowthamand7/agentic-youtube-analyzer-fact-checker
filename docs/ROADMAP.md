# Roadmap

Status as of 2026-11-02. Items marked **HELD** are decided and designed but intentionally not started.

## Done

- **Report v2.** Summary-first report built from a structured object (`agent/report.py`): TL;DW, "What you'll learn"
  (key ideas with timestamps and a checked/disputed badge), "What's wrong or doubtful", "From the comments",
  "Elsewhere on the web", and dynamic sections the model picks per video (steps, comparison, timeline, numbers).
  Claims are importance-ranked and capped by video length; the single average score is replaced by
  "N of M checked claims hold up" (weighted by importance).
- **Personalization removed.** The "I already know this" button, the knowledge profile and the matching model call were
  built and then removed: they did not add enough value to justify the extra step and latency.

## HELD

### #3 Hosted API for other people

Needed before anyone but the author can use it.

- Accounts and auth.
- Quotas and rate limits. One analysis costs roughly 6-12 Tavily searches plus 4-6 model calls and takes 45-110 s.
- Shared per-video cache so a popular video is analyzed once.
- CORS and an allowed-origins list for the extension; HTTPS; secrets management (API keys must stay server-side).
- Transcript fetching from a server IP gets blocked at scale (see #4 for the plan).
- Cost controls: cap concurrent runs, kill switch, per-user daily budget.

### #4 Chrome extension (side panel)

- Manifest V3 with `sidePanel` and a `contextMenus` entry on YouTube video links (`youtube.com/watch*`, `/shorts/*`);
  `contextMenus.onClicked` is a user gesture, so it may open the side panel.
- Side panel reuses the existing UI. It loads no external scripts, which suits the extension's content-security rules.
- **Fetch transcripts in the user's own browser** (their IP) and send the text to the API, instead of the server
  fetching from YouTube. This avoids the IP-block problem entirely. It depends on YouTube's internal endpoints, so keep
  the server-side fetch as a fallback and expect occasional breakage.
- Progressive display: stream the summary first (about 10 s), then fill in fact-checks.
- Chrome Web Store review, privacy policy, permission justification.

## Known gaps in what is built

- **Run-to-run variance.** Re-running the same video can pick different claims and verdicts (the MCP video went from
  "3 of 5 hold up" to "4 of 4"). A re-run replaces the saved report. Consider keeping history, or fixing the claim list
  on re-run.
- **Verdict calibration.** Most verdicts are Verified or Mostly True. There is no ground-truth set to measure accuracy;
  an evaluation set of videos with known errors would show whether "what's wrong" is trustworthy.
- **Reports saved before v2** still render in the old Markdown layout until re-run.
- **Reddit** is off until API access is approved (see `SETUP_AND_KEYS.md`).
- **Single user.** The profile and saved reports are local files.
