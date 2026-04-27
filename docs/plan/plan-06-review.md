# Review — Plan 06: Faster CPU STT Engine Research

**Reviewed:** 2026-04-27
**Plan:** docs/plan/plan-06.md
**Reviewer:** subagent (read-only inspection + code audit)

---

## Executive Summary

Plan 06 presents a **sound phased evaluation strategy** with clear acceptance gates (Gold/Silver/Bronze/Fail) and correctly identifies that RTF ≤ 0.3 on 4 vCPU is an aggressive target. The plan respects the non-negotiable invariants (`condition_on_previous_text=False`, `word_timestamps=True`).

However, the plan suffers from a **critical scope omission** (Distil-Whisper), an **optimistic effort estimate**, and **gaps in benchmark statistical rigor** that could produce irreproducible or misleading results. If executed as-written, the research will miss the single most promising CPU-only speedup path and may waste 2–3 days on whisper.cpp before discovering that Distil-Whisper large-v3 would have solved the problem in hours.

**Verdict:** Revise before execution. The core structure (phased gates, decision matrix, benchmark harness) is good; the candidate list and effort estimate need correction.

---

## 1. Research Methodology

| Aspect | Rating | Finding |
|--------|--------|---------|
| Overall approach | ✅ Sound | Controlled A/B comparison of RTF and WER against a fixed baseline is the correct methodology. |
| Statistical rigor | ⚠️ MEDIUM | The plan mentions "mean, median, P95" but does not specify **how many repetitions per condition**, **confidence intervals**, or **outlier rejection**. On a VPS, a single noisy-neighbor event can shift RTF by 20 %. |
| Environment control | ⚠️ MEDIUM | No mention of disabling CPU frequency scaling, checking thermal throttling, or running benchmarks at quiet VPS times. |
| Baseline verification | ⚠️ LOW | The plan assumes RTF = 1.15 is the ground truth but does not prescribe re-measuring the baseline at the start of every benchmark session to detect VPS drift. |

**Recommendation:** Add a pre-flight protocol: (a) re-run baseline (beam=5, large-v3-turbo) 5× and verify RTF is within ±10 % of historical average; (b) run each candidate condition **N = 5 times**, drop the min/max, report mean ± stddev; (c) document VPS CPU steal time (`cat /proc/stat`) at session start.

---

## 2. Scope — Candidate Coverage

| Aspect | Rating | Finding |
|--------|--------|---------|
| Beam-size tuning | ✅ Correct | Candidates 1 and 2 cover greedy and beam=3. |
| Alternative runtimes | ✅ Correct | whisper.cpp Q4_0 and Q5_1 are valid candidates. |
| Model-size sweep | ⚠️ MEDIUM | `small` and `medium` are listed, but `small` is correctly flagged as high-risk. |
| **Distil-Whisper** | ❌ **CRITICAL** | **Completely absent.** Distil-Whisper (`distil-large-v3`, 756 M params, distilled from large-v3) is specifically engineered for 2–4× faster inference with ≤ 1 pp WER regression. On CPU with CTranslate2, it is the single most important candidate for this plan. |
| CTranslate2 compute types | ❌ **HIGH** | The plan locks `compute_type="int8"` without testing `int8_float16` or `int8_float32`. On modern x86_64 CPUs, `int8_float16` can be faster with negligible accuracy loss. |
| Thread-count optimization | ❌ **HIGH** | The plan assumes 4 threads is optimal. A 1×, 2×, 4×, 8× thread sweep takes < 1 hour and could reveal that 2 threads gives 90 % of the throughput with lower latency variance — critical for the single-FIFO-lock design. |
| VAD chunking | ❌ **HIGH** | `faster-whisper` supports `vad_filter=True`, which skips non-speech segments. On WhatsApp audio with long silence gaps, this can reduce effective audio duration by 20–40 % with **zero model change** and **zero WER impact**. |
| ONNX/OpenVINO | ⚠️ MEDIUM | Correctly deprioritized, but the plan should explicitly note that OpenVINO requires Intel AVX-512 and verify VPS CPU flags before spending any time on it. |
| `best_of` / `temperature` | ⚠️ LOW | The API already accepts `temperature` (form field) but **does not pass it to the engine** (dead parameter). The plan should evaluate whether `temperature=0.0` + greedy reduces variance vs. `temperature=0.2`. |

**Recommendation:**
1. **Add Candidate 8: Distil-Whisper (`distil-large-v3`)** — expect RTF ~0.35–0.55, WER regression ≤ 1 pp, setup complexity = Low (drop-in replacement in faster-whisper).
2. **Add Task 0: CPU feature detection** — `grep flags /proc/cpuinfo | head -1` to verify AVX2/AVX-512 before whisper.cpp estimates are trusted.
3. **Add Task 1b: Thread-count sweep** — benchmark 1, 2, 4, 8 threads with baseline model/beam_size=5.
4. **Add Task 1c: VAD filter benchmark** — `vad_filter=True` vs `False` on the reference dataset.
5. **Add Task 1d: CTranslate2 compute_type sweep** — `int8`, `int8_float16`, `int8_float32` (if supported by CPU).

These four additions cost ~4 hours and could obsolete the entire whisper.cpp track.

---

## 3. Benchmark Design

| Aspect | Rating | Finding |
|--------|--------|---------|
| RTF formula | ✅ Correct | `wall_time / audio_duration` is the standard definition. |
| WER tooling | ✅ Correct | `jiwer` is the standard library; normalization rules are sensible. |
| Warm-up | ⚠️ MEDIUM | "3 transcriptions before timing" is vague — what audio? Same clip or silent WAV? |
| Duration buckets | ✅ Correct | 3 s, 10 s, 30 s, 60 s cover the operational range well. |
| Reference dataset | ❌ **HIGH** | `tests/fixtures/pt-br-reference/` does not exist, and the plan provides **no acquisition strategy**. Synthetic sine waves (used by `loadtest.py`) yield meaningless WER. |
| Statistical test | ❌ **HIGH** | No mention of paired statistical testing. If greedy WER is 14.2 % and beam=5 WER is 13.1 %, is that significant? The plan needs a `wilcoxon` or paired t-test threshold (e.g., p < 0.05). |
| Isolation | ⚠️ MEDIUM | Running benchmark harness in the same Python process as the model risks GC pauses and interpreter overhead. Subprocess invocation per candidate is cleaner. |

**Recommendation:**
- Specify the reference dataset source: either (a) Common Voice Portuguese test split (~100 clips), or (b) 20 operator-recorded WhatsApp-style clips (noisy, code-switching, compressed). The latter is more representative but requires labor to transcribe.
- Add `jiwer` to `pyproject.toml` dependencies before the plan starts.
- Require **N = 5 runs per condition**, report mean ± stddev, and flag conditions where stddev > 15 % of mean for re-run.
- Use `subprocess` isolation for each candidate to avoid cross-candidate memory/CPU cache pollution.

---

## 4. Practicality

| Aspect | Rating | Finding |
|--------|--------|---------|
| VPS accessibility | ✅ Correct | Plan explicitly targets the deployed VPS (10.10.10.208). |
| Docker compatibility | ✅ Correct | All candidates are evaluated against Docker constraint. |
| whisper.cpp integration | ⚠️ MEDIUM | The plan admits Python bindings are "immature" but still budgets only 2 days for integration + benchmarking + formatter adapter. Realistically, writing a `WhisperCppEngine` class with output format parity (`verbose_json`, `srt`, `vtt` word timestamps) is 3–5 days alone. |
| Disk space | ⚠️ LOW | No mention of disk budget. `large-v3` (CTranslate2) + `large-v3` (GGUF Q4_0) + `large-v3` (GGUF Q5_1) + `medium` + `small` + Distil-Whisper could exceed 5 GB. The VPS may not have that headroom. |
| Dependency readiness | ⚠️ MEDIUM | `jiwer` is not in `pyproject.toml`. The plan assumes it is available. |

**Recommendation:**
- Add a pre-flight disk-space check (`df -h /`).
- Add `jiwer` to `pyproject.toml` as a dev/test dependency.
- Split Phase 2 into "Phase 2a: whisper.cpp prototype (1 day, go/no-go)" and "Phase 2b: Full integration (2–3 days, only if 2a passes Gold gate)."

---

## 5. Risk Assessment

| Aspect | Rating | Finding |
|--------|--------|---------|
| Invariant protection | ✅ Correct | `condition_on_previous_text=False` and `word_timestamps=True` are correctly called out as non-negotiable. |
| WER regression ceiling | ✅ Correct | 3 pp absolute is a sensible threshold; 2 pp for Gold is appropriately tighter. |
| Greedy + hallucination | ⚠️ MEDIUM | The plan notes that greedy increases hallucination risk, but it does not propose testing the **interaction** of greedy + `condition_on_previous_text=False`. These two are not independent: greedy commits to early errors, and `condition_on_previous_text=False` prevents the model from self-correcting via context. The combined effect on long audio (> 60 s) could be worse than the sum of individual effects. |
| whisper.cpp word timestamps | ⚠️ MEDIUM | Correctly flagged, but no acceptance test is defined for timestamp quality (e.g., max word jitter vs. baseline). |
| Memory pressure | ⚠️ LOW | Loading multiple models sequentially for A/B testing is safe, but the plan should warn against loading two models simultaneously on a 4 GB budget. |
| Unattainable target | ✅ Correct | Phase 3 correctly provides an exit strategy if RTF ≤ 0.3 is impossible. |

**Recommendation:**
- Add a "Long-audio hallucination stress test" to the benchmark harness: a single 120 s clip with beam=1 and `condition_on_previous_text=False`, measuring repetition rate (unique words / total words).
- Define a word-timestamp quality gate: for whisper.cpp candidates, word-level timestamps must have mean absolute error ≤ 0.3 s vs. baseline on the reference dataset.

---

## 6. Dependencies on Plan 3

| Aspect | Rating | Finding |
|--------|--------|---------|
| Explicit dependency | ❌ **MEDIUM** | The plan assumes the VPS is operational but does not **explicitly list Plan 3 completion** as a prerequisite. If Plan 3's env-var fixes (compose.yaml ↔ config.py mismatch) are not deployed, the benchmark may run with wrong settings (e.g., default threads=4 when compose intended 8), invalidating results. |
| Hardware consistency | ⚠️ LOW | The plan should record the exact VPS instance ID / CPU model so that future re-benchmarks are comparable. |

**Recommendation:**
- Add a "Prerequisites" section: "Plan 03 must be complete and `health.yml` must pass on 10.10.10.208. Benchmarks must run against the exact compose configuration deployed by Plan 03."
- Capture `lscpu` output into `docs/plan/plan-06-benchmark-results.md`.

---

## 7. Effort Estimate Accuracy (3–5 days)

| Phase | Plan Estimate | Realistic Estimate | Gap |
|-------|---------------|-------------------|-----|
| Phase 1 (greedy + harness) | Day 1 | 1 day | ✅ |
| Thread/compute/VAD sweep | Not planned | 0.5 day | — |
| Distil-Whisper benchmark | Not planned | 0.5 day | — |
| Phase 2 (whisper.cpp) | Days 2–3 | 3–5 days | **+2 days** |
| Phase 3 (documentation) | Day 4+ | 0.5 day | ✅ |
| Reference dataset curation | Not planned | 0.5–1 day | — |
| **Total** | **3–5 days** | **5–8 days** | **Optimistic by ~2 days** |

**Recommendation:** Revise estimate to **5–8 days**. If Distil-Whisper hits Gold on Day 1, the plan can early-exit at 2 days; if whisper.cpp is pursued, it will consume the full 8 days.

---

## 8. Output Quality

| Aspect | Rating | Finding |
|--------|--------|---------|
| Gold/Silver/Bronze gates | ✅ Excellent | Clear, actionable, and tied to business value. |
| Phased approach | ✅ Excellent | Phase 1 early-exit is the right ROI strategy. |
| Decision matrix | ⚠️ MEDIUM | The 1–5 scores and 30 %/25 % weights appear arbitrary. For example, greedy gets WER preservation = 3 and `small` gets 1, but no data supports that 3:1 ratio — it is intuition. That is acceptable for a pre-research plan, but the matrix should be regenerated from **actual benchmark numbers** in the results doc. |
| Files to modify | ⚠️ MEDIUM | The list is correct but **incomplete**: `src/compadre/api.py` hardcodes `beam_size=5` (line 158) and must also be modified to read `settings.whisper_beam_size`. |
| Code changes staged | ✅ Correct | The plan correctly limits code changes to config + harness + optional engine adapter. |

**Recommendation:**
- Add `src/compadre/api.py` to "Files to Modify" (pass `settings.whisper_beam_size` to `engine.transcribe()`).
- Annotate the decision matrix: "Pre-research heuristic scores — will be replaced with measured scores in `plan-06-benchmark-results.md`."

---

## Code Observations (Current State)

1. **`src/compadre/api.py:158`** — `beam_size=5` is hardcoded. The plan's config change is insufficient; the API layer must also consume the setting.
2. **`src/compadre/api.py:60`** — `temperature: float = Form(0.0)` is accepted from clients but **never passed to `engine.transcribe()`**. This is dead code that should either be wired or removed. The plan could evaluate whether exposing temperature helps greedy decoding quality.
3. **`src/compadre/config.py`** — No `whisper_beam_size` field exists yet. Adding it is straightforward.
4. **`src/compadre/stt.py`** — Already accepts `beam_size` parameter (good). `warm_up()` uses `beam_size=1` regardless of production setting — this is fine for warm-up but should be documented.
5. **`pyproject.toml`** — `jiwer` is absent. Must be added to test/dev dependencies.
6. **`tests/fixtures/`** — Directory does not exist. Must be created.
7. **`scripts/loadtest.py`** — Generates synthetic sine waves. These are useless for WER measurement but fine for throughput/RTF load testing. The plan should not conflate load-test audio with WER-reference audio.

---

## Actionable Recommendations (Prioritized)

### 🔴 Must Do Before Execution

1. **Add Distil-Whisper (`distil-large-v3`) as Candidate 8.** This is the highest-ROI path and its omission is the plan's biggest flaw. It is a drop-in faster-whisper model change with expected RTF ~0.4–0.55 and WER regression ≤ 1 pp.
2. **Add `whisper_cpu_threads` sweep and `vad_filter` benchmark to Phase 1.** These are zero-integration-cost optimizations that could yield 20–40 % speedup before any model change.
3. **Add `jiwer` to `pyproject.toml` and create `tests/fixtures/pt-br-reference/README.md`** with a specific acquisition strategy (Common Voice Portuguese test split or operator-recorded clips).
4. **Add `src/compadre/api.py` to the files-to-modify list** and specify that `beam_size=5` must become `beam_size=settings.whisper_beam_size`.
5. **Explicitly list Plan 03 completion as a prerequisite** with a health-check verification step.

### 🟡 Should Do

6. **Revise effort estimate to 5–8 days** and add early-exit criteria (e.g., "If Distil-Whisper or greedy hits Silver by end of Day 1, skip whisper.cpp").
7. **Add statistical protocol:** N = 5 runs, drop min/max, report mean ± stddev, re-run if stddev > 15 %.
8. **Add CPU feature detection task** (`grep avx2 /proc/cpuinfo`) before whisper.cpp phase.
9. **Add a long-audio hallucination stress test** (120 s clip, measure repetition rate) for beam=1 candidates.
10. **Add disk-space pre-flight check** before downloading multiple models.

### 🟢 Nice to Have

11. **Evaluate `temperature` wiring** in `api.py` — either pass it to the engine or remove the dead parameter.
12. **Add CTranslate2 `compute_type` sweep** (`int8`, `int8_float16`) to Phase 1.
13. **Annotate the decision matrix** as heuristic-only and commit to regenerating it from measured data.

---

## Conclusion

Plan 06 is **structurally sound** but **materially incomplete** in candidate coverage. The phased gate strategy (Gold/Silver/Bronze) is excellent and should be preserved. The critical fix is adding **Distil-Whisper** and **VAD filtering** to the scope — without them, the research risks spending days on whisper.cpp integration when a simpler, lower-risk path may already satisfy the Silver or Gold gate. Once revised, the plan is executable and will produce actionable, evidence-based recommendations.
