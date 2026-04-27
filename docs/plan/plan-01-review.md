# Review — Implementation Plan 01: Documentation Audit & Correction

**Reviewer:** assistant  
**Date:** 2026-04-27  
**Plan file:** `docs/plan/plan-01.md`  
**Master index:** `docs/plan/README.md`

---

## Executive Summary

Plan 01 is a **well-structured but dangerously stale planning artifact**. At the time of this review, **approximately 90 % of the described work has already been executed** — the docs have been rewritten, the audit checklist exists and is marked complete, and 8 source-code fixes were applied as follow-on work. The plan describes a forward-looking 4–6 hour task load; in reality a senior developer executing it today would spend most of their time discovering that the tasks are already done. The plan should be **retired or converted to a retrospective**, and the master plan index should reflect its completion.

---

## 1. Completeness

| Aspect | Rating | Finding |
|--------|--------|---------|
| Doc coverage | ⚠️ Medium | Lists 13 doc files; there are actually **14** `.md` files in `docs/` (the plan omits `checkpoint-2026-04-26.md` from its count, though it appears later as Task 12). `README.md` and `AGENTS.md` are correctly included. |
| Stale-reference inventory | ✅ Good | The grep inventory (kokoro, qwen3, espeak, `/v1/audio/speech`, env vars, `src/comadre`, etc.) is exhaustive and copy-paste runnable. It correctly covers every TTS→STT mutation vector observed in the codebase history. |
| Non-doc artifacts | ❌ Missing | **Out of scope** incorrectly excludes code changes, yet the audit found critical stale references in `src/compadre/` (`exceptions.py` dead classes, `rate_limit.py` TTS constant names, `api.py` empty-string language bug, `health.py` macOS memory units). A documentation plan that does not verify docs against code will miss doc→code drift. Also missing: `compose.yaml`, `.env.example`, `deploy/landing/index.html`, and `deploy/ansible/inventory/*.yml.example` — all of which contained stale TTS refs that were fixed during execution. |
| Historical docs | ✅ Good | Correctly identifies `compadre-brief.md`, `compadre-development-plan.md`, and `checkpoint-2026-04-26.md` as historical, prescribing banners rather than body edits. This preserved provenance. |
| Acceptance criteria | ⚠️ Medium | Most tasks include a grep-based acceptance test. However, Task 7 (`monetization.md`) references line numbers (`docs/deployment-guide.md:480-504`) which are fragile and already drifted by the time the audit ran. |

### Critical gap: Pre-execution state unknown
The plan was written assuming the docs were 100 % Comadre TTS. In reality:
- `api-architecture.md`, `api-reference.md`, `configuration-reference.md`, `deployment-guide.md`, `troubleshooting.md`, `local-testing-guide.md`, `monetization.md`, `rate-limiting.md`, and `proxmox-vm-setup.md` were **already rewritten** for STT.
- The only remaining fixes were minor: adding historical banners, correcting a GitHub URL in the privacy template, and adding "Last updated" dates.

**Severity: Critical.** A senior developer receiving this plan cold would waste 1–2 hours verifying that the described rewrites are in fact already complete.

---

## 2. Execution Clarity

| Aspect | Rating | Finding |
|--------|--------|---------|
| Task granularity | ✅ Good | 16 tasks with explicit file targets, change type (complete rewrite / major update / targeted fix / review-only), and per-task effort. Easy to parallelise. |
| Command examples | ✅ Good | Every task that needs one includes exact `grep` commands for before/after validation. The multipart curl examples in Tasks 1–2 are accurate. |
| Cross-references | ⚠️ Medium | Task 7 references `docs/deployment-guide.md:480-504` for LGPD procedures — line-number citations are brittle. Content-based anchors (`#lgpd-operator-obligations`) would be more stable. |
| Scope boundaries | ⚠️ Medium | "Out of scope: Coding, code changes" is too rigid. The plan should at least mandate a **read-only code cross-check** (e.g., "Verify every env var in `configuration-reference.md` exists in `src/compadre/config.py`"). Task 3 does this for env vars, but the principle isn't applied elsewhere (e.g., health response shape, engine parameters). |
| Rollback / safety | ❌ Missing | No mention of how to back out a bad doc rewrite (git branch, snapshot). For a plan that involves complete rewrites of 6 files, this is a notable omission. |

---

## 3. Risk Assessment

| Risk (from plan) | Assessment |
|------------------|------------|
| Monetization.md LGPD URL change affecting operators | **Low / mitigated** — Correctly identified. The URL was updated to `github.com/docg1701/compadre` during execution. |
| Rewritten docs describe "should be" not "is" | **Medium / realised** — The plan itself contributed to this risk: Tasks 1–2 described health responses with `engines.whisper.compute_type` and `cpu_threads` fields that **do not exist** in `src/compadre/health.py`. These were caught and fixed during execution (Discrepancies #6 and #9 in `audit-checklist.md`). |
| ffmpeg/model path mismatch | **Low / not realised** — Paths were already correct in the rewritten docs. |
| Historical doc context lost | **Low / mitigated** — Banners preserved provenance. |
| Line-number drift in monetization.md | **Low / not realised** — Content-based search was used instead. |
| README cost tables inaccurate | **Low / not realised** — Numbers were already updated. |
| `compadre-keys` binary name confusion | **Low / not realised** — Name was already correct. |

### Risks the plan missed

| Risk | Severity | Why it matters |
|------|----------|--------------|
| **Docs already updated** | Critical | The plan's fundamental assumption — "docs lag behind" — was false for most files. This is the single biggest execution risk and it was not listed. |
| **Source-code stale refs leak into docs** | High | `exceptions.py` still had `VoiceNotFoundError`; `rate_limit.py` had `_SPEECH_PER_IP_LIMIT`. Fixing docs without fixing code creates a documented system that doesn't exist. |
| **Landing page and config files** | Medium | `deploy/landing/index.html`, `.env.example`, and `compose.yaml` all had TTS-era content. Excluding them from scope left visible stale artifacts. |
| **Per-task effort sums to ~12 h vs plan total of 4–6 h** | High | See §6 below. |

---

## 4. Dependencies and Sequencing

| Aspect | Rating | Finding |
|--------|--------|---------|
| Upstream dependencies | ✅ Correct | States "No upstream dependencies." True — this is pure documentation work. |
| Parallel execution | ✅ Correct | Allows Tasks 1–5 (the heavy rewrites) to run in parallel. Historical docs (9–10, 12) and targeted fixes (7–8, 11) can run in parallel with everything else. Task 16 (checklist) correctly depends on all others. |
| Fit in cycle strategy | ✅ Correct | README.md positions Plan 1 as Cycle 1, parallel with Plan 2 (API Functional Validation). This is sound: corrected docs inform test authoring, and tests validate doc accuracy. |
| Exit criteria | ⚠️ Weak | The plan exits when the grep inventory returns zero hits and the checklist is filled. It does not define who signs off (self-certified vs peer review) or how discrepancies between docs and code are escalated. |

---

## 5. Conformance to Master Plan Index

| Requirement | Status | Notes |
|-------------|--------|-------|
| Self-contained | ✅ | Plan can be read and executed without referring to other plans (except the master index for context). |
| Effort listed in index | ✅ | 4–6 h is recorded in `README.md` table. |
| Focus documented | ✅ | "Fix stale Comadre TTS refs, update all docs" matches the content. |
| Cycle assignment | ✅ | Cycle 1, parallel with Plan 2. |
| Checkpoint mandate | ⚠️ | Master index requires a `docs/checkpoint-YYYY-MM-DD.md` after each plan. `checkpoint-2026-04-26.md` exists but pre-dates this specific plan's execution; `audit-checklist.md` acts as a de facto checkpoint but is not in the required format. |

---

## 6. Effort Estimate Accuracy

| Task | Estimated | Realistic (for stale docs) |
|------|-----------|---------------------------|
| 1 api-architecture.md | 2 h | 2 h (if actually rewriting) |
| 2 api-reference.md | 2 h | 2 h (if actually rewriting) |
| 3 configuration-reference.md | 1 h | 1 h |
| 4 deployment-guide.md | 2 h | 2 h |
| 5 troubleshooting.md | 1.5 h | 1.5 h |
| 6 local-testing-guide.md | 1.5 h | 1.5 h |
| 7 monetization.md | 30 m | 30 m |
| 8 rate-limiting.md | 15 m | 15 m |
| 9 compadre-brief.md | 5 m | 5 m |
| 10 compadre-development-plan.md | 5 m | 5 m |
| 11 proxmox-vm-setup.md | 15 m | 15 m |
| 12 checkpoint-2026-04-26.md | 5 m | 5 m |
| 13 claude-code-instruction-quality-guide.md | 10 m | 10 m |
| 14 README.md | 30 m | 30 m |
| 15 AGENTS.md | 20 m | 20 m |
| 16 audit-checklist.md | 15 m | 15 m |
| **Sum** | **~12 h 15 m** | **~12 h 15 m** |
| **Plan claim** | **4–6 h** | **2–3× underestimate** |

Even accounting for parallel execution (a senior developer can rewrite 2–3 docs simultaneously in different editors), 4–6 h is optimistic for **6 complete rewrites + 2 major updates + 6 targeted fixes + cross-checking against code**. The actual execution record in `audit-checklist.md` shows **8 code fixes and 17 discrepancies** were resolved — work that fell outside the plan's scope but was necessary to make the docs coherent.

**Severity: High.** If this plan were handed to a contractor with a 6-hour budget, they would either deliver incomplete work or overrun.

---

## Findings Summary with Severity

| # | Finding | Severity | Recommendation |
|---|---------|----------|----------------|
| F1 | Plan assumes docs are 100 % TTS stale; in reality they were already ~90 % rewritten. Executing the plan verbatim wastes effort. | **Critical** | Mark Plan 1 as **COMPLETE** in the master index. Convert `plan-01.md` to a retrospective or archive. Do not assign it for execution. |
| F2 | Per-task effort sums to ~12 h; plan claims 4–6 h. | **High** | For future plans, sum individual estimates before publishing the total. If parallel execution reduces wall-clock time, state the parallelism assumption explicitly (e.g., "12 h effort, 6 h wall-clock with 2 parallel streams"). |
| F3 | Excludes code changes from scope, but doc accuracy requires code cross-checks. Stale code refs (exceptions, rate-limit constant, API language handling) were found during execution. | **High** | Add a "Doc→Code verification" task to every documentation plan: run the examples, check health response shapes against `src/compadre/health.py`, and verify env vars against `config.py`. |
| F4 | Missing non-markdown artifacts: `compose.yaml`, `.env.example`, `deploy/landing/index.html`, Ansible inventory examples. | **Medium** | Expand documentation audit scope to "all human-readable project files that describe the system" — not just `.md` files. |
| F5 | Task 7 uses line-number citations (`deployment-guide.md:480-504`) which drift. | **Medium** | Replace line numbers with heading anchors or content-based search strings. |
| F6 | No rollback strategy for complete rewrites. | **Medium** | Add a note: "Before rewriting, commit the current docs to a branch (`git checkout -b docs/pre-stt-rewrite`) so operators can diff or revert." |
| F7 | Missing sign-off / exit owner. Plan exits when grep returns zero, with no peer-review gate. | **Low** | Add: "Final sign-off: a second developer runs the validation checklist independently and initials the audit checklist." |
| F8 | Plan document itself lacks a "Last updated" date. | **Low** | Add `**Last updated:** 2026-04-27` to `plan-01.md` if it is retained as a historical artifact. |

---

## Positive Elements (Preserve for Future Plans)

1. **Grep inventory is exemplary.** The before/after grep commands are exact, runnable, and cover every stale-term category. This should become a template for all audit-style plans.
2. **Task-level acceptance criteria.** Every task states what "done" looks like (grep returns zero, curl examples are runnable, etc.).
3. **Historical preservation.** The decision to banner rather than rewrite historical docs (`compadre-brief.md`, etc.) was correct and should be repeated.
4. **Risk table format.** The Likelihood × Mitigation structure is clear and scannable.
5. **Validation checklist at the bottom.** The 14-point checklist is a strong close-out instrument — it just needs to be checked against reality before the plan is frozen.

---

## Recommended Actions

1. **Immediate:** Update `docs/plan/README.md` — change Plan 1 status from "planned" to `✅ COMPLETE (2026-04-27)`.
2. **Immediate:** Append a banner to the top of `docs/plan/plan-01.md`:
   ```markdown
   > ⚠️ **Executed** — This plan was completed on 2026-04-27. All tasks were finished;
   > see `docs/plan/audit-checklist.md` for the execution record and discrepancy log.
   > Do not re-execute.
   ```
3. **For Plan 2 (API Functional Validation):** Review whether it also assumes a pre-rewrite state. Update its prerequisites to read: "Requires Plan 1 docs to be STT-consistent."
4. **For future documentation plans:** Add a **15-minute "pre-flight" task** that runs the grep inventory *before* writing task descriptions. If the inventory returns near-zero hits, the plan should pivot to "spot-check and verify" rather than "complete rewrite."
