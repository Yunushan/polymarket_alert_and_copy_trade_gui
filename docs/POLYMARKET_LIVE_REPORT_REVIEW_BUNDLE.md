# Polymarket Live Validation Review Bundle

The review bundle is a sanitized operator artifact for one stored live-validation report.
It is designed for human review, audit handoff, and release evidence. It does not contain
the raw stored report payload and it never changes static Polymarket coverage tiers.

## Exports

After a report is stored, export either format from the local API or the React Live Safety
report controls:

```powershell
curl http://127.0.0.1:8765/api/polymarket/live-validation/reports/<REPORT_KEY>/review.json
curl http://127.0.0.1:8765/api/polymarket/live-validation/reports/<REPORT_KEY>/review.md
```

The JSON export is for machines and CI artifacts. The Markdown export is for operator
review notes and release checklists.

## Contents

Each bundle contains:

| Section | Purpose |
| --- | --- |
| `report` | Key metadata, label, source, stored timestamp, redacted payload hash, source-file provenance, and compact report summary. |
| `schema_validation` | Accepted/rejected state, mode, report type, errors, warnings, and accepted modes. |
| `duplicate_history` | `duplicate_of`, duplicate skip count, retained duplicate-import audit events, source file names, and last duplicate import. |
| `promotion_review` | Credential/funded promotion status, accepted evidence rows, stage-gate claims, blockers, and required evidence fields. |
| `operator_commands` | Source CLI commands copied from the report, such as safe live probes or credentialed-read verification commands. |
| `coverage_tier_mapping` | Static public/credential/funded coverage-state counts plus how this report maps to each tier for review. |

## Safety Rules

- `static_coverage_mutated` is always `false`.
- `funded_execution_exposed` is always `false`.
- The raw report payload is not included.
- Secrets already redacted before storage remain redacted in the bundle.
- A bundle can identify `candidate_only` evidence, but it never establishes
  `credential_live_verified` or `funded_live_verified`. A trusted workflow and exact-byte
  attestation are required before production verification; an operator decision or local
  code/docs update alone is insufficient.

## Verification

`python verify.py` builds a temporary stored report, exports the review bundle, confirms
the JSON/Markdown contain schema, provenance, duplicate history, promotion blockers,
operator commands, and coverage-tier mapping, and confirms seeded secrets are absent.

For operator acceptance/rejection of the bundle, use the separate decision ledger in
`docs/POLYMARKET_LIVE_REPORT_DECISION_LEDGER.md`. The review bundle is evidence; the
decision ledger records the operator decision and still does not mutate static coverage.
Accepted ledger decisions can be summarized into a no-automerge manual patch proposal
with `docs/POLYMARKET_LIVE_REPORT_PROMOTION_PROPOSAL.md`.
