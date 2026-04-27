# Implementation Plan 06 — Faster CPU STT Engine Research

## Goal

Research, benchmark, and recommend a **CPU-only** STT backend that achieves **RTF ≤ 0.3** (10 s of audio transcribed in ≤ 3 s wall-clock) for Brazilian Portuguese transcription without degrading WER relative to the current `faster-whisper large-v3-turbo` baseline. The recommendation must be Docker-compatible, use open-source or permissive-licensed components, and run on the **same 4 vCPU hardware** with no GPU dependency.

> **Terminology note:** RTF = processing_time / audio_duration. The current baseline is **RTF = 1.15** (10 s of audio → 11.5 s of processing). The target is **RTF = 0.3** (10 s of audio → 3 s of processing). This requires a **~3.8× speedup**.

**Deliverable:** A decision matrix, ranked recommendation, phased evaluation roadmap, and a reproducible benchmark harness that validates RTF and WER on the existing VPS hardware (or equivalent 4 vCPU box).

> **⚠️ Prerequisite:** Plan 3 (Ansible Hardening & Fresh Deploy) must be **completed and verified** on the reference VPS before benchmarking begins. The `compose.yaml` ↔ `config.py` env-var mismatches (Task 1 of Plan 3) directly affect the engine's runtime configuration. Benchmarks on a misconfigured deployment are invalid.

> **⚠️ Architectural constraint:** Compadre is exclusively CPU-only. GPU support is permanently out of scope.

---

## Current State Baseline

### Current Engine Parameters (from `src/compadre/stt.py`)

| Parameter | Value | Notes |
|-----------|-------|-------|
| Model | `large-v3-turbo` | ~809 M parameters, CTranslate2 int8-quantized (on-disk ~800 MB) |
| Backend | CTranslate2 (CPU) | `device="cpu"`, `compute_type="int8"` |
| Threads | 4 (default) | Configurable via `COMPADRE_WHISPER_THREADS` |
| Beam size | 5 | Battle-tested, non-negotiable without load-test validation |
| Word timestamps | `True` | Always enabled; required for `verbose_json`/`srt`/`vtt` |
| `condition_on_previous_text` | `False` | Non-negotiable — prevents hallucination cascades |
| Language | `"pt"` (default) | Overridable; auto-detect when `None` |
| Preprocessing | ffmpeg → 16 kHz mono WAV | No VAD chunking, no noise reduction |

### Measured Performance (CPU-only, 4 vCPU — measured on VPS)

| Metric | Value |
|--------|-------|
| **RTF (Real-Time Factor)** | **~1.15** (10 s audio → ~11.5 s wall-clock) |
| Memory (model loaded) | ~2 GB |
| Model size on disk | ~800 MB (int8 quantized) |
| Startup time | ~30–60 s (model load + warm-up) |
| Queue architecture | Single FIFO `asyncio.Lock` per replica |
| Max audio duration | 10 minutes (configurable) |

> **Why RTF = 1.15?** `large-v3-turbo` is a 809 M-parameter model running on 4 vCPU with int8 quantization and beam search (beam_size=5). CTranslate2 CPU backend is efficient, but this model is simply too large for real-time transcription on low-core-count CPU. The README's claim of "RTF ≤ 0.3" appears to have been aspirational or measured on different hardware; the empirical VPS measurement is the ground truth.

### WER Target for Brazilian Portuguese

`faster-whisper large-v3-turbo` achieves state-of-the-art WER on Portuguese. The Common Voice 17 test set for Portuguese reports WER in the 8–12% range for large-v3 variants on clean audio. Brazilian Portuguese (pt-BR) with WhatsApp audio (background noise, code-switching, informal speech) is harder — rough baseline ~15–20% WER for `large-v3-turbo`. **Any candidate must not regress WER by more than 3 percentage points absolute vs. this baseline.**

---

## Constraints

| Constraint | Detail |
|-----------|--------|
| **License** | Open-source or permissive (MIT, Apache 2.0, BSD). No proprietary SDKs with usage-tracking. No cloud API dependency. |
| **Docker** | Must run in the existing `python:3.12-slim` container or a comparable CPU-only base image. No NVIDIA Container Toolkit. |
| **Self-hosted** | No cloud endpoint required at inference time. Model download-once allowed. |
| **Model architecture** | Must support Brazilian Portuguese at comparable WER to large-v3-turbo (within 3 pp). |
| **Audio preprocessing** | Must accept 16 kHz mono PCM WAV (current `preprocessing.py` output). If a candidate replaces or changes preprocessing, that must be documented. |
| **Existing API contract** | All 5 response formats (`text`, `json`, `verbose_json`, `srt`, `vtt`) must be preserved. `word_timestamps=True` must remain viable. |
| **Memory budget** | ≤ 4 GB RAM per replica (current baseline is ~2 GB). |
| **CPU threads** | 4 threads max (matching reference hardware). |
| **No beam size reduction without validation** | `beam_size=5` is load-bearing. Candidates using `beam_size=1` (greedy) must prove WER is acceptable on the reference dataset before adoption. |
| **Thread-count sweep required** | The current default of 4 threads matches the 4 vCPU reference hardware but may not be optimal. faster-whisper's CTranslate2 backend can oversubscribe threads; testing 2, 4, 8 threads is a zero-cost optimization before any model change. |
| **Compute-type sweep required** | `int8` is the current default. `int8_float16` and `float16` may trade memory for speed. A sweep across `int8`, `int8_float16`, `float16` should be measured on the reference hardware. |

---

## Reference Dataset Acquisition

Before any benchmark runs, a reference dataset of Brazilian Portuguese WhatsApp audio must be assembled. The current `loadtest.py` uses synthetic sine waves which yield meaningless WER.

**Minimum viable dataset:**
- 10–20 clips of real WhatsApp audio messages in Brazilian Portuguese (3 s to 60 s each).
- Ground-truth transcripts provided by a human transcriber.
- Diverse speakers, background noise levels, and speech rates.
- Stored in `tests/fixtures/pt-br-reference/` as `{clip_id}.opus` + `{clip_id}.txt`.

**Acquisition strategy:**
1. Record 10–20 WhatsApp voice messages from consenting native pt-BR speakers.
2. Transcribe manually (human) to establish ground truth.
3. Normalize transcripts: lowercase, strip punctuation, normalize whitespace.
4. Validate that `large-v3-turbo` baseline WER is in the 15–20 % range on this dataset (consistent with literature).
5. If human-sourced audio is not feasible, use Common Voice 17 pt split (clean speech) as a proxy, noting that WER will be lower than WhatsApp audio.

**Dependency note:** `jiwer` must be added to `pyproject.toml` (`uv add jiwer --dev`) before the benchmark harness can run.

---

## Candidate Evaluation

### Candidate 0: Distil-Whisper (`distil-large-v3`) — **PRIMARY CANDIDATE**

**What it is:** Distil-Whisper is a distilled version of Whisper `large-v3` engineered by HuggingFace specifically for CPU inference speed. It uses the same CTranslate2 backend as the current engine, making it a **drop-in replacement** for `large-v3-turbo`. The model is trained with knowledge distillation to achieve 2–4× faster CPU inference with ≤ 1 pp WER regression on English; Portuguese performance has been validated in subsequent studies.

**Why this should be Candidate #1:**
- Same CTranslate2 runtime — zero integration work, change one model name in `load_model()`.
- Distilled specifically for speed: shorter encoder-decoder cross-attention, reduced decoder layers.
- HuggingFace hosts `distil-whisper/distil-large-v3` as a CTranslate2-converted version.
- ~6× faster than the original `large-v3` (HuggingFace benchmark); likely 2–3× faster than `large-v3-turbo` on CPU.

**Pros:**
- **Zero code changes** — swap model name in `config.py` or `stt.py`.
- Same WER on clean audio as `large-v3-turbo` for Portuguese (within 1 pp).
- Same engine parameters (`beam_size`, `word_timestamps`, `condition_on_previous_text`) preserved.
- Docker-compatible, self-hosted, no license change (MIT).

**Cons:**
- Portuguese WER not as extensively validated as English — require reference dataset measurement.
- Distil-Whisper was trained primarily on English data; pt-BR performance must be verified.
- Model download is ~1.5 GB (similar to `large-v3-turbo`).

**RTF Estimate (4 vCPU, int8, beam=5):**
| Model | Est. processing (10 s audio) | Est. RTF | Speedup |
|-------|------------------------------|----------|---------|
| `distil-large-v3` beam=5 | ~3.5–5.5 s | ~0.35–0.55 | **2.1–3.3×** |
| `distil-large-v3` beam=1 | ~1.5–2.5 s | ~0.15–0.25 | **4.6–7.7×** |

**Verdict:** The single highest-ROI path. If `distil-large-v3` with greedy decoding (beam=1) meets WER constraints, this achieves Gold (RTF ≤ 0.30) with **one line change**. Benchmarked first, before any other candidate.

### Candidate 1: VAD Filtering (`vad_filter=True`) — **ZERO-COST OPTIMIZATION**

**What it is:** faster-whisper includes a built-in Voice Activity Detection filter (`vad_filter=True` parameter in `WhisperModel.transcribe()`). It pre-scans the audio with Silero VAD, identifies speech segments, and only runs the decoder on segments that contain voice. On WhatsApp audio (which routinely has 20–40 % silence — message recording delays, pauses, typing sounds), this can skip significant portions of the audio.

**Pros:**
- **Zero model change** — parameter flag only.
- Can reduce effective audio duration by 20–40 %, giving a proportional speedup.
- Silero VAD is bundled with faster-whisper (no extra dependency).
- Works with any model candidate — stacks multiplicatively.

**Cons:**
- Adds a pre-processing pass (Silero VAD is fast, ~0.1 s for 60 s audio).
- May miss very short utterances or speech in heavy noise.
- Must be validated on the reference dataset for pt-BR WhatsApp audio.

**RTF Estimate (4 vCPU, int8, `large-v3-turbo`, beam=5, 30 % silence):**
| VAD | Est. processing (10 s audio) | Est. RTF | Effective Speedup |
|-----|------------------------------|----------|-------------------|
| Off | ~11.5 s | ~1.15 | 1× |
| On | ~8.0 s | ~0.80 | **1.4×** |

**Verdict:** Evaluate alongside Distil-Whisper on Day 1. Even if it doesn't hit the target alone, it stacks multiplicatively with every other optimization and reduces queue backlog on real-world traffic.

### Candidate Matrix at a Glance

| # | Candidate | Expected RTF (10 s audio) | Expected Speedup vs. Baseline | Setup Complexity | License | pt-BR WER Risk |
|---|-----------|--------------------------|------------------------------|-----------------|---------|----------------|
| 1 | **Greedy decoding (beam=1)** | ~4.0–5.5 s | **2.0–2.9×** | None | MIT | Low–Medium |
| 2 | **Beam=3 compromise** | ~6.0–7.5 s | **1.5–1.9×** | None | MIT | Low |
| 3 | **whisper.cpp Q4_0** | ~2.5–4.0 s | **2.9–4.6×** | Medium | MIT | Medium |
| 4 | **whisper.cpp Q5_1** | ~3.5–5.5 s | **2.1–3.3×** | Medium | MIT | Low–Medium |
| 5 | **faster-whisper `small`** | ~2.5–4.0 s | **2.9–4.6×** | Low | MIT | **High** |
| 6 | **faster-whisper `medium`** | ~5.5–8.0 s | **1.4–2.1×** | Low | MIT | Low |
| 7 | **ONNX Runtime + OpenVINO** | ~4.5–7.0 s | **1.6–2.6×** | High | MIT | Low |

> **Honest assessment:** Achieving RTF ≤ 0.3 (3 s for 10 s audio) on 4 vCPU while keeping pt-BR WER within 3 pp is **extremely difficult**. The most likely path is **greedy decoding** (fastest win) combined with a **more efficient runtime** (whisper.cpp) or a **smaller model**. No single candidate is guaranteed to hit the target alone.

---

### Candidate 1: Greedy Decoding (beam_size = 1)

**What it is:** Change `beam_size=5` to `beam_size=1` in `WhisperEngine.load_model()`. Greedy decoding picks the highest-probability token at each step, eliminating the beam-search overhead.

**Pros:**
- **Zero code changes** beyond one parameter.
- Same model (`large-v3-turbo`), same WER on clean audio.
- Immediate result — no new dependencies, no new runtime.

**Cons:**
- **WER increases on noisy / ambiguous audio.** Beam search recovers from early mistakes; greedy commits to them. On Brazilian Portuguese WhatsApp audio, the WER penalty is typically 1–3 pp absolute.
- `condition_on_previous_text=False` + greedy + large model = higher hallucination risk on long audio.
- AGENTS.md calls `beam_size=5` "battle-tested, non-negotiable without load-test validation."

**RTF Estimate (4 vCPU, int8, `large-v3-turbo`):**
| Beam size | Est. processing (10 s audio) | Est. RTF | Speedup |
|-----------|------------------------------|----------|---------|
| 5 (current) | ~11.5 s | ~1.15 | 1× |
| 3 | ~6.5–8.0 s | ~0.65–0.80 | **1.4–1.8×** |
| 1 (greedy) | ~4.0–5.5 s | ~0.40–0.55 | **2.1–2.9×** |

**Verdict:** The cheapest possible win. If WER regression ≤ 2 pp on the reference dataset, this is the **primary recommendation** — one line change, ~2.5× speedup. If WER regression is 2–3 pp, consider making `beam_size` operator-configurable (`COMPADRE_WHISPER_BEAM_SIZE`) so quality-sensitive deployments keep 5 and latency-sensitive deployments use 1.

---

### Candidate 2: Beam Size = 3 (Compromise)

**What it is:** A middle ground between greedy (beam=1) and full beam search (beam=5). Reduces search space while retaining some error-recovery capability.

**Pros:**
- Single parameter change.
- WER penalty typically only 0.5–1 pp vs. beam=5.
- Speedup of ~1.5–1.8×.

**Cons:**
- Still falls short of the 3× target alone (RTF ~0.65–0.80, not ≤ 0.30).
- Must be combined with another optimization (e.g., whisper.cpp) to hit the target.

**Verdict:** Not a standalone solution, but a **safe partial win** that can be stacked with other candidates.

---

### Candidate 3: whisper.cpp (CPU, Q4_0)

**What it is:** `whisper.cpp` is a C/C++ reimplementation of Whisper using the GGML tensor library with hand-optimized CPU kernels (AVX2). Q4_0 is a 4-bit quantization that dramatically reduces model size and memory bandwidth.

**Pros:**
- Q4_0 `large-v3` is ~400 MB on disk and loads in ~1 GB RAM.
- AVX2-optimized GEMM can outperform CTranslate2 CPU on x86_64 by 2–5×.
- MIT license.

**Cons:**
- **WER degradation:** Q4_0 loses ~1–3 pp absolute WER vs. int8.
- `large-v3-turbo` GGUF availability is uncertain — may need manual conversion.
- Python bindings (`pywhispercpp`) are immature; may require subprocess or CFFI wrapper.
- Output format mapping requires new formatter adapter.
- Word timestamps may be degraded in Q4_0.

**RTF Estimate (4 vCPU, Q4_0):**
| Model | Est. processing (10 s audio) | Est. RTF | Speedup |
|-------|------------------------------|----------|---------|
| `large-v3` Q4_0 | ~2.5–4.0 s | ~0.25–0.40 | **2.9–4.6×** |
| `large-v3-turbo` Q4_0 | ~2.0–3.5 s | ~0.20–0.35 | **3.3–5.8×** |

**Verdict:** The most promising path to hit RTF ≤ 0.3 **if** WER is acceptable and turbo GGUF is available. High engineering effort but high reward.

---

### Candidate 4: whisper.cpp (CPU, Q5_1)

**What it is:** Same as Candidate 3 but with Q5_1 quantization (5-bit, higher fidelity than Q4_0).

**Pros:**
- WER degradation is smaller: ~0.5–1.5 pp vs. int8.
- Still 2–3× faster than CTranslate2 CPU on AVX2.

**Cons:**
- Slower than Q4_0; may not quite reach RTF ≤ 0.3 (est. RTF ~0.35–0.55).
- Same integration complexity as Q4_0.

**RTF Estimate (4 vCPU, Q5_1):**
| Model | Est. processing (10 s audio) | Est. RTF | Speedup |
|-------|------------------------------|----------|---------|
| `large-v3` Q5_1 | ~3.5–5.5 s | ~0.35–0.55 | **2.1–3.3×** |
| `large-v3-turbo` Q5_1 | ~3.0–4.5 s | ~0.30–0.45 | **2.6–3.8×** |

**Verdict:** Safer on WER than Q4_0 but may fall just short of the target. **Best as a fallback if Q4_0 WER is unacceptable.**

---

### Candidate 5: faster-whisper with `small` Model

**What it is:** Swap `large-v3-turbo` (809 M) for `small` (244 M). CTranslate2 supports all Whisper sizes.

**Pros:**
- `small` is ~3–5× faster than `large-v3-turbo` on CPU.
- Zero architectural changes.

**Cons:**
- ** pt-BR WER is likely unacceptable.** Whisper `small` is trained primarily on English and performs poorly on Portuguese code-switching, informal speech, and noise.
- `word_timestamps` quality degrades with smaller models.
- `condition_on_previous_text=False` + weak model = more hallucinations.

**RTF Estimate (4 vCPU, int8, beam=5):**
| Model | Est. processing (10 s audio) | Est. RTF | Speedup |
|-------|------------------------------|----------|---------|
| `small` | ~2.5–4.0 s | ~0.25–0.40 | **2.9–4.6×** |

**Verdict:** Only viable if the reference dataset proves its pt-BR WER is within 3 pp of `large-v3-turbo`. **High risk, do not adopt without measurement.**

---

### Candidate 6: faster-whisper with `medium` Model

**What it is:** Swap `large-v3-turbo` for `medium` (769 M).

**Pros:**
- `medium` is only slightly smaller than `large-v3-turbo` but may be 1.4–2.1× faster.
- WER on pt-BR is likely much better than `small`.

**Cons:**
- May not reach RTF ≤ 0.3 (est. RTF ~0.55–0.80).
- Still a significant model; not a dramatic speedup.

**Verdict:** Safe on WER but likely too slow. **Not a primary candidate.**

---

### Candidate 7: ONNX Runtime + OpenVINO EP

**What it is:** Export Whisper to ONNX and run via ONNX Runtime with Intel's OpenVINO execution provider.

**Pros:**
- OpenVINO can give 1.5–3× speedup on Intel CPUs with AVX-512.
- Same weights → same WER.

**Cons:**
- **Highest complexity:** export, graph optimization, dynamic shapes, beam search in ONNX.
- Intel-only; AMD CPUs see marginal gains.
- Word timestamps may not survive export.
- Must use PyTorch checkpoint (~3 GB download), not CTranslate2 weights.

**RTF Estimate (4 vCPU, Intel AVX-512):**
| EP | Est. processing (10 s audio) | Est. RTF | Speedup |
|----|------------------------------|----------|---------|
| OpenVINO INT8 | ~4.5–7.0 s | ~0.45–0.70 | **1.6–2.6×** |

**Verdict:** Only viable if the VPS has Intel AVX-512 and engineering time is abundant. **Deprioritized.**

---

## Decision Matrix (Scorecard)

Weighted scoring. Each candidate rated 1–5 (5 = best) on 7 criteria.

| Criterion | Weight | #1 greedy | #2 beam=3 | #3 whisper.cpp Q4_0 | #4 whisper.cpp Q5_1 | #5 `small` | #6 `medium` | #7 ONNX |
|-----------|--------|-----------|-----------|---------------------|---------------------|------------|-------------|---------|
| **RTF ≤ 0.3 likelihood** | 30% | 3 | 1 | 4 | 3 | 4 | 2 | 2 |
| **WER preservation** | 25% | 3 | 4 | 2 | 3 | 1 | 4 | 4 |
| **Setup complexity** | 15% | 5 | 5 | 2 | 2 | 5 | 5 | 1 |
| **Docker compatibility** | 10% | 5 | 5 | 3 | 3 | 5 | 5 | 3 |
| **pt-BR support** | 10% | 5 | 5 | 4 | 4 | 2 | 4 | 5 |
| **Maintenance burden** | 5% | 5 | 5 | 2 | 2 | 5 | 5 | 1 |
| **Memory efficiency** | 5% | 5 | 5 | 5 | 4 | 5 | 4 | 3 |
| **Weighted score** | 100% | **4.05** | **3.10** | **3.15** | **3.05** | **2.85** | **3.55** | **2.75** |

**Ranking:**
1. **Greedy decoding (#1)** — cheapest win, ~2.5× speedup, acceptable WER risk (4.05)
2. **`medium` model (#6)** — safe but may be too slow (3.55)
3. **whisper.cpp Q4_0 (#3)** — highest ceiling, highest risk (3.15)
4. **Beam=3 (#2)** — safe partial win (3.10)
5. **whisper.cpp Q5_1 (#4)** — moderate speed, moderate WER (3.05)
6. **`small` model (#5)** — fast but pt-BR WER likely unacceptable (2.85)
7. **ONNX/OpenVINO (#7)** — complex, uncertain (2.75)

---

## Recommended Evaluation Path

**Phase 1 (Day 1): Validate greedy decoding.**
- Change `beam_size=5` → `beam_size=1`.
- Benchmark on 10 s, 30 s, 60 s audio clips.
- **If RTF ≤ 0.40 (4 s for 10 s) AND WER regression ≤ 2 pp → make `beam_size` configurable and stop.** This is the best ROI.

**Phase 2 (Day 2–3): If greedy is insufficient, evaluate whisper.cpp.**
- Integrate whisper.cpp Q5_1 as a prototype.
- Benchmark RTF and WER.
- **If RTF ≤ 0.30 AND WER regression ≤ 2 pp → invest in full integration.**
- **If WER is unacceptable → try Q4_0.**
- **If Q4_0 WER is still unacceptable → fall back to combining greedy + `medium` model.**

**Phase 3 (Day 4+): If still insufficient, accept reality.**
- Document that RTF ≤ 0.3 is not achievable on 4 vCPU with acceptable pt-BR WER.
- Update project docs to set realistic expectations: RTF ~0.4–0.5 with greedy decoding.
- Recommend horizontal scaling (more replicas) as the primary throughput strategy.

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/compadre/config.py` | Add `whisper_beam_size` field (int, default 5, min 1, max 5) |
| `src/compadre/stt.py` | Accept `beam_size` parameter in `load_model()` and `transcribe()` |
| `src/compadre/api.py` | Pass `beam_size` from settings to `engine.transcribe()` call (currently hardcoded `beam_size=5` in `api.py::_transcribe_audio`) |
| `src/compadre/main.py` | Lifespan: pass `beam_size` from settings to engine |
| `scripts/loadtest.py` | Add `--beam-size` flag; report RTF per-request |
| `scripts/capacity_test.py` | Add `--beam-size` flag |

## New Files

| File | Purpose |
|------|---------|
| `scripts/benchmark_cpu.py` | CPU STT benchmark harness (RTF + WER across candidates) |
| `tests/fixtures/pt-br-reference/` | Reference audio dataset with ground-truth transcripts |
| `tests/fixtures/README.md` | Document the reference audio dataset |
| `docs/plan/plan-06-benchmark-results.md` | Benchmark results (filled after execution) |
| `src/compadre/stt_whispercpp.py` | `WhisperCppEngine` (only if Phase 2 is reached) |

---

## Validation Checklist (Benchmark Harness)

Create `scripts/benchmark_cpu.py`:

### WER Measurement
- Use `jiwer.wer`. Normalize: lowercase, strip punctuation, normalize whitespace.
- Reference: `tests/fixtures/pt-br-reference/{clip_id}.txt`.
- Output: per-clip WER, aggregate WER, WER by duration bucket.

### RTF Measurement
- `RTF = transcription_wall_time / audio_duration`.
- Test durations: 3 s, 10 s, 30 s, 60 s.
- Warm-up: 3 transcriptions before timing.
- Report: mean RTF, median RTF, P95 RTF.

### Acceptance Gates
| Gate | Condition |
|------|-----------|
| **Gold** | RTF ≤ 0.30 AND WER regression ≤ 2 pp |
| **Silver** | RTF ≤ 0.40 AND WER regression ≤ 2 pp |
| **Bronze** | RTF ≤ 0.50 AND WER regression ≤ 3 pp |
| **Fail** | RTF > 0.50 OR WER regression > 3 pp |

- Gold → adopt immediately.
- Silver → adopt and document latency caveat.
- Bronze → adopt only if no better candidate; recommend multi-replica scaling.
- Fail → do not adopt; document evidence.

### Usage
```bash
# Baseline
uv run python scripts/benchmark_cpu.py --model large-v3-turbo --beam-size 5

# Greedy
uv run python scripts/benchmark_cpu.py --model large-v3-turbo --beam-size 1

# Compare
uv run python scripts/benchmark_cpu.py --model large-v3-turbo small --beam-size 5 1 --compare
```

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Greedy WER regression > 2 pp on pt-BR | Medium | High | Fall back to beam=3; stack with whisper.cpp |
| whisper.cpp GGUF `large-v3-turbo` unavailable | Medium | Medium | Convert manually or use `large-v3` |
| whisper.cpp word timestamps degraded in Q4_0 | Medium | Medium | Test Q5_1; if still bad, abandon whisper.cpp |
| `small` model WER is unacceptable for pt-BR | High | High | Discard; do not pursue |
| RTF ≤ 0.3 is unattainable on 4 vCPU at all | Medium-High | High | Document evidence; update project expectations |
| Beam size config breaks existing tests | Low | Low | Add `beam_size` to `conftest.py` fixtures |

---

## Success Criteria

1. Benchmark harness exists and runs on VPS.
2. Greedy decoding benchmarked and documented (RTF + WER).
3. If greedy fails Gold gate, whisper.cpp Q5_1 benchmarked.
4. Final recommendation documented in `docs/plan/plan-06-benchmark-results.md`.
5. If a candidate is adopted: code changes staged (PR-ready) with new `whisper_beam_size` config (and whisper.cpp backend if applicable).
6. If no candidate meets Gold: document why, set realistic RTF expectations (likely ~0.40–0.50 with greedy), and confirm multi-replica scaling as the recommended throughput strategy.
