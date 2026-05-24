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
