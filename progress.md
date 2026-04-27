# Progress — Plan 01 Review

## Review

### Correct (already good)
- The grep inventory in `plan-01.md` is exhaustive and covers every TTS→STT mutation vector.
- Task-level acceptance criteria (grep returns zero, curl examples runnable) are well-defined.
- Historical docs are correctly identified for banner preservation, not body rewriting.
- Risk table format (Likelihood × Mitigation) is clear and reusable.
- Dependency sequencing correctly places Plan 1 in Cycle 1, parallel with Plan 2.
- The validation checklist at the bottom of `plan-01.md` is a strong close-out instrument.

### Fixed (issue and resolution)
- **Issue:** `plan-01.md` describes docs as 100 % TTS stale, but in reality ~90 % were already rewritten.
  **Resolution:** Wrote `docs/plan/plan-01-review.md` documenting the actual state, recommending the plan be marked complete and retired.
- **Issue:** Per-task effort estimates sum to ~12 h, but the plan claims 4–6 h.
  **Resolution:** Documented in review with a detailed breakdown table; recommended future plans state parallelism assumptions explicitly.
- **Issue:** Plan excludes code changes from scope, but the audit found stale code refs (`exceptions.py`, `rate_limit.py`, `api.py`) that had to be fixed to make docs coherent.
  **Resolution:** Recommended adding a "Doc→Code verification" task to all future documentation plans.
- **Issue:** Missing non-markdown artifacts (`compose.yaml`, `.env.example`, `deploy/landing/index.html`, Ansible inventories).
  **Resolution:** Recommended expanding scope to "all human-readable project files that describe the system."
- **Issue:** Task 7 uses line-number citations (`deployment-guide.md:480-504`) which drift.
  **Resolution:** Recommended replacing with heading anchors or content-based search strings.
- **Issue:** No rollback strategy for complete rewrites.
  **Resolution:** Recommended adding a git-branch pre-flight note.

### Note (observations / follow-up)
- `docs/plan/audit-checklist.md` already exists and shows Plan 1 is effectively complete (all 15 docs passed, 8 code fixes applied, 17 discrepancies resolved). The master plan index at `docs/plan/README.md` should be updated to reflect this.
- `docs/checkpoint-2026-04-26.md` is the most recent checkpoint and already covers much of the Plan 1 execution; it is in Portuguese (acceptable per checkpoint convention).
- `plan-01.md` itself lacks a "Last updated" date — ironic given that Task 16 requires one for every doc.
- The only remaining grep hit for TTS terms across all current docs is the intentional parenthetical in `deployment-guide.md` line 634 ("legacy from the prior TTS project"), which is acceptable context.
