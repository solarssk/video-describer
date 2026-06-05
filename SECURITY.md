# Security

## Reporting a vulnerability

If you find a security issue — hardcoded credentials, an injection vector, an unsafe dependency — please don't open a public GitHub issue.

Instead, use GitHub's private reporting:
**[Report a vulnerability →](https://github.com/solarssk/video-describer/security/advisories/new)**

Or reach out directly via email if you prefer.

## What to expect

- You'll get an acknowledgement within a few days
- If the issue is valid, a fix will be prioritised and a patched version released
- You'll be credited in the changelog unless you'd prefer otherwise

## Scope

This tool runs locally on your machine and never exposes a public endpoint. The main things worth reporting:

- API keys leaking through logs or output files
- Unsafe handling of file paths from user input
- Dependencies with known CVEs not yet caught by Dependabot

## Out of scope

- Issues in third-party dependencies (report those upstream; Dependabot will handle updates here)
- Theoretical vulnerabilities without a realistic attack path on a local-only tool

## CodeQL `py/path-injection` — known triaged findings

video-describer is a local macOS desktop tool served on `localhost`. The user intentionally selects their own media folders via a native macOS file picker (NSOpenPanel).

Starting with v0.4.4, the primary picker flow uses a server-side **selection registry** — the UI sends a `selection_id` token, not a raw filesystem path, to processing endpoints. CodeQL sees this registry flow as clean.

Three legacy compatibility paths remain and are intentionally kept:

- **CLI usage** — `python describe_videos.py /path/to/media`
- **Manual path entry** in the local UI (typed directly, no picker)
- **Backward compatibility** with local tooling that posts `{"path": "..."}` directly

CodeQL flags these as `py/path-injection`. They have been individually triaged and dismissed in the Security tab (alerts #41, #42 as *false positive*; #44, #45 as *won't fix*) with audit comments. The rule remains enabled globally so future unexpected path injection issues are still detected.

A follow-up task for v0.5 will consider separating CLI raw paths from web UI paths and optionally requiring `selection_id` for all web endpoints.
