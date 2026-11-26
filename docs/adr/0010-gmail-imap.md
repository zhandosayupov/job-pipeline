# 0010. Gmail over IMAP with an app password

Date: 2026-11-26 · Status: accepted (implemented in S5)

## Context
OAuth apps left in "Testing" get refresh tokens that expire after about 7 days; `gmail.readonly` is
a restricted scope, and verification is not worth it for one user.

## Decision
IMAP over TLS with a Google app password (requires 2-Step Verification). Read with IMAP search;
create drafts with IMAP `APPEND` to the Drafts folder. Code never sends mail in v1.

## Consequences
The app password is a full-mailbox credential: it lives only in `secrets.env`.
