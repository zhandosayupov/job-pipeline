# 0011. LinkedIn for discovery only

Date: 2026-11-26 · Status: accepted

## Decision
LinkedIn is a source, not an applier. Postings are fetched without login at low volume
(`python-jobspy`), the company's own ATS (Greenhouse, Lever, Ashby, Workday) is resolved from the
offsite apply link or known board tokens, and the application goes through that ATS.
Easy-Apply-only postings become a notification for manual applying.

## Consequences
No automation touches the owner's LinkedIn account.
