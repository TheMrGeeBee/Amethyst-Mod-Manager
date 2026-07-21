---
paths:
  - ".github/workflows/*.yml"
  - "Changelog.txt"
---

# Release & CI/CD conventions

- Three workflows: `test-build.yml`, `build.yml`, `release.yml`. Don't consolidate them without checking why they were split.
- `release.yml` triggers on `v*` tags.
- Changelog extraction parses `- v{major.minor.patch}` headers exactly — any deviation (extra text on the header line, different bullet/heading style) breaks the release notes extraction. Verify a new header matches this format before committing.
- Fork's version always wins over upstream on conflict (e.g. `2.0.4-beta.X` beats `2.0.3-beta.X`) — don't "fix" a version bump downward to match upstream.
