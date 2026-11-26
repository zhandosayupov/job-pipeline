# Later: after the main sprints (S0–S6)

Ideas and decisions parked on purpose. Each item becomes an ADR or a sprint task when picked up.

## Further decisions

- **Salary expectation statistics.** Collect salary data per role, level and country (from the
  ranges published in postings we already collect, plus public salary sources) and build a
  reference table. With it, `policy.yaml: sensitive_answers.compensation` could move from `ask`
  to a market-based answer instead of asking the owner every time. Until then: always ask
  (ADR 0016), or `posting_range` for postings that publish a range.

## Tooling

- **Security review workflow** (`anthropics/claude-code-security-review`): needs an Anthropic API
  key (`CLAUDE_API_KEY`), not the subscription token. Worth adding before S4/S5, when code starts
  acting on the owner's behalf.
- **CI failure auto-fix workflow** (`ci-failure-auto-fix.yml` from the claude-code-action
  examples).
- **Stricter review for risky paths** (`pr-review-filtered-paths.yml`) on `adapters/appliers/` and
  `adapters/inbox/`.
