# 0015. JSON logs to journald with redaction

Date: 2026-11-26 · Status: accepted

## Decision
Structured JSON lines on stderr; systemd sends them to journald, which handles rotation
(`SystemMaxUse` set by the provisioning script). A logging filter masks every value from
`secrets.env`, email addresses and phone numbers before a record is formatted.
