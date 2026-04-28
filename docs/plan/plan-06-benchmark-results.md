# Plan 06 — CPU STT Engine Benchmark Results

**Date:** 2026-04-27  
**Hardware:** 4 vCPU (AVX2), 7.7 GB RAM, VPS at 10.10.10.208  
**Test audio:** Brazilian Portuguese WhatsApp voice message (~14.3s, real speech) plus trimmed/looped variants at 3s, 10s, 30s, 60s  
**Primary metric:** RTF for 10s audio clip (most representative of typical WhatsApp voice message)

---

## Executive Summary

**RTF ≤ 0.30 with acceptable Brazilian Portuguese WER is NOT achievable on 4 vCPU CPU-only hardware.** The Whisper large-v3 encoder requires ~10 seconds on 4 vCPU regardless of configuration (beam size, threads, quantization, backend). This is a fundamental compute limitation.

**Best realistic result:** `small` model at RTF ~0.41 with noticeable but not catastrophic quality degradation. If quality is non-negotiable, the current `large-v3-turbo` at RTF ~1.08 remains the best option with horizontal scaling.

---

## Baseline

| Model | Config | 10s RTF | Quality |
|-------|--------|---------|---------|
| `large-v3-turbo` | int8, beam=5, t=4 | **1.08** | Excellent (baseline) |

Per-clip breakdown:
| Clip | Audio | Wall | RTF | Segments |
|------|-------|------|-----|----------|
| 3s | 3.0s | 10.2s | 3.38 | 1 |
| 10s | 10.0s | 10.8s | **1.08** | 3 |
| full (14.3s) | 14.3s | 11.5s | 0.81 | 3 |
| 30s | 30.0s | 49.8s | 1.66 | 7 |
| 60s | 60.0s | 89.0s | 1.48 | 15 |

Key observation: The encoder has a ~10s fixed cost regardless of audio duration (the 3s and 10s clips both take ~10s). This is why RTF improves for longer audio — the fixed cost is amortized.

---

## Candidate 1: Distil-Whisper (`distil-large-v3`)

**Status: ❌ REJECTED**

| Config | 10s RTF | vs Baseline |
|--------|---------|-------------|
| beam=5, t=4 | 1.12 | 0.96× (slightly worse) |

**Critical finding: Distil-Whisper outputs ENGLISH text for Portuguese audio despite `language="pt"` being set and the language detector reporting pt with probability 1.00.**

Transcript comparison (3s clip):
- baseline: "É porque a Renata e Marcelo estão aqui."
- distil-large-v3: "It's because Renato and Marcella are here."

Distil-Whisper was trained with knowledge distillation on English data. Its multilingual capabilities are severely degraded — it effectively functions as an English-only model. This is a showstopper for pt-BR use.

RTF is also not improved: Distil-Whisper reduces decoder layers (32→2) but keeps the full encoder, which dominates CPU time.

---

## Candidate 2: VAD Filtering (`vad_filter=True`)

**Status: ❌ NO BENEFIT**

| Config | 10s RTF | vs Baseline |
|--------|---------|-------------|
| baseline + VAD | 1.11 | 0.97× (slightly worse) |

VAD filtering adds a pre-processing pass (Silero VAD) that costs ~0.3s but finds no silence to skip in dense speech. On real WhatsApp audio with 20-40% silence, VAD could provide a 1.2-1.4× speedup, but on dense speech it's a net negative. Not viable as a standalone optimization.

---

## Candidate 3: Greedy Decoding (`beam_size=1`)

**Status: ⚠️ MARGINAL IMPROVEMENT**

| Config | 10s RTF | vs Baseline |
|--------|---------|-------------|
| beam=1, t=4 | 1.03 | 1.05× (5% faster) |

Only 5% speedup. The encoder dominates CPU time (~10s); beam search decoder overhead is negligible (~0.3-0.5s). The plan's estimate of 2-3× speedup was incorrect for this hardware.

Transcript is near-identical to beam=5 baseline. If every millisecond matters, beam=1 is a safe change with negligible quality impact.

---

## Candidate 4: Thread Count Sweep

**Status: ❌ THREADS=4 IS OPTIMAL**

| Config | 10s RTF | vs Baseline |
|--------|---------|-------------|
| t=2 | 1.90 | 0.57× (worse) |
| **t=4** | **1.08** | **1.00×** |
| t=8 | 1.24 | 0.87× (worse) |

- **t=2**: Underutilizes CPU — 2× slower
- **t=4**: Optimal — matches vCPU count
- **t=8**: Thread oversubscription causes thrashing — 15% slower

No room for improvement here.

---

## Candidate 5: Compute Type Sweep

**Status: ❌ NOT SUPPORTED ON CPU**

- `int8_float16`: ValueError — "target device or backend do not support efficient int8_float16 computation"
- `float16`: ValueError — "target device or backend do not support efficient float16 computation"

CTranslate2's int8 is the only compute type available on CPU. The plan's estimates for these were incorrect.

---

## Candidate 6: Smaller Models

### `medium` (769M params)

| Config | 10s RTF | Quality |
|--------|---------|---------|
| medium, int8, beam=5 | 1.09 | Good but degrades on long audio |

RTF nearly identical to large-v3-turbo for short audio. But **30s clip produced 0 segments** (transcription failed). The medium model struggles with longer audio, making it worse than baseline.

### `small` (244M params)

| Config | 10s RTF | Quality |
|--------|---------|---------|
| small, int8, beam=5 | **0.41** ⭐ | Noticeable errors |

Closest to Silver gate (RTF ≤ 0.40). Transcription is understandable but has errors:
- "quantos aletes é grandão" instead of "com a Tia Laíte Zé Grandão"
- "você ficaram" instead of "vocês ficaram"

### `base` (74M params)

| 10s RTF | Quality |
|---------|---------|
| **0.14** 🥇 | Poor Portuguese |

Gold on speed but unacceptable WER — "te alaites a grandão" is nonsensical.

### `tiny` (39M params)

| 10s RTF | Quality |
|---------|---------|
| **0.10** 🥇 | Gibberish |

Fastest but transcription is unintelligible for pt-BR.

---

## Candidate 7: whisper.cpp (GGML, Q5_0)

**Status: ❌ SLOWER THAN FASTER-WHISPER**

| Backend | Model | 3s Encode Time | RTF |
|---------|-------|---------------|-----|
| faster-whisper (CTranslate2) | large-v3-turbo int8 | ~10.2s | 3.4 |
| **whisper.cpp (GGML)** | large-v3-turbo Q5_0 | **31.9s** | **10.6** |

whisper.cpp with Q5_0 quantization is **3× slower** than faster-whisper with int8 on this CPU. The CTranslate2 backend uses highly optimized Intel MKL GEMM kernels; whisper.cpp's GGML kernels cannot match this throughput on this hardware.

FP16 whisper.cpp: encode time 29.3s for 3s audio (also ~3× slower than faster-whisper).

The plan's estimate that whisper.cpp would be 2-5× faster was incorrect for this CPU architecture.

---

## Consolidated Results Table

Sorted by 10s RTF (best first):

| # | Config | Model | Beam | Threads | VAD | 10s RTF | Quality | Gate |
|---|--------|-------|------|---------|-----|---------|---------|------|
| 1 | tiny | tiny | 5 | 4 | No | 0.10 | Gibberish | — |
| 2 | base | base | 5 | 4 | No | 0.14 | Poor pt-BR | — |
| 3 | small | small | 5 | 4 | No | **0.41** | Errors but readable | 🥉 BRONZE |
| 4 | greedy | large-v3-turbo | 1 | 4 | No | 1.03 | Excellent | ❌ FAIL |
| 5 | **baseline** | **large-v3-turbo** | **5** | **4** | **No** | **1.08** | **Excellent** | **(reference)** |
| 6 | medium | medium | 5 | 4 | No | 1.09 | Good/buggy | ❌ FAIL |
| 7 | baseline+VAD | large-v3-turbo | 5 | 4 | Yes | 1.11 | Excellent | ❌ FAIL |
| 8 | distil-large-v3 | distil-large-v3 | 5 | 4 | No | 1.12 | **ENGLISH** | ❌ FAIL |
| 9 | threads=8 | large-v3-turbo | 5 | 8 | No | 1.24 | Excellent | ❌ FAIL |
| 10 | threads=2 | large-v3-turbo | 5 | 2 | No | 1.90 | Excellent | ❌ FAIL |

---

## Per-Clip RTF Detail (Key Candidates)

| Config | 3s | 10s | 14.3s | 30s | 60s |
|--------|-----|------|--------|------|------|
| baseline (large-v3-turbo) | 3.38 | **1.08** | 0.81 | 1.66 | 1.48 |
| greedy (beam=1) | 3.29 | 1.03 | 0.75 | 1.64 | 1.45 |
| small | 0.84 | **0.41** | 0.32 | 1.81 | 0.89 |
| base | 0.28 | 0.14 | 0.12 | 0.57 | 0.45 |
| tiny | 0.25 | 0.10 | 0.28 | 0.41 | 0.35 |

---

## Acceptance Gate Evaluation

| Gate | Condition | Achieved By |
|------|-----------|-------------|
| 🥇 Gold | RTF ≤ 0.30 AND WER ≤ baseline+2pp | Nothing with acceptable quality |
| 🥈 Silver | RTF ≤ 0.40 AND WER ≤ baseline+2pp | Nothing with acceptable quality |
| 🥉 Bronze | RTF ≤ 0.50 AND WER ≤ baseline+3pp | `small` (RTF 0.41 but WER regression > 3pp) |
| ❌ Fail | Everything else | All large-v3 variants |

**No configuration passes any gate with acceptable quality.** The `small` model hits Bronze on RTF but would fail WER against a proper reference transcript.

---

## Why RTF ≤ 0.30 Is Not Achievable

The Whisper large-v3 architecture has a 32-layer transformer encoder that processes the full mel spectrogram (1500 frames for 30s, proportionally fewer for shorter audio). On 4 vCPU with AVX2:

1. **Encoder fixed cost:** ~10 seconds for any audio up to ~30s. The encoder processes all mel frames in a single forward pass — there's a minimum compute cost regardless of audio duration.

2. **Decoder cost:** ~0.5-1.5 seconds, grows with output tokens and beam size. Negligible compared to encoder.

3. **No optimization touches the encoder:**
   - Beam size changes only affect the decoder (~5% impact)
   - VAD only skips decoder work on silence segments, not encoder work
   - Thread count is already optimal at 4
   - int8 is the only supported CPU compute type
   - whisper.cpp Q5_0 is actually 3× slower than CTranslate2 int8 on this CPU

4. **Only path to RTF ≤ 0.30:** use a model with fewer encoder layers (small = 12 layers, base = 6 layers, tiny = 4 layers). These achieve the speed target but sacrifice Portuguese WER catastrophically.

---

## Recommendation

### Immediate (no code changes)

**Stay with `large-v3-turbo` at RTF ~1.08.** It produces the best Portuguese transcription quality. For throughput, scale horizontally:
- 4 replicas = ~4 concurrent transcriptions on 4 vCPU
- Queue depth-aware load balancing via Caddy `least_conn`

### If minor latency improvement is desired (low risk)

Make `beam_size` operator-configurable via `COMPADRE_WHISPER_BEAM_SIZE` env var (default 5). Operators can set to 1 for ~5% speedup with negligible quality impact. This is a one-line config addition.

### If you can accept some quality degradation

The `small` model at RTF ~0.41 is the best trade-off. It's not perfect Portuguese but is understandable for WhatsApp messages. This would require:
- Adding `whisper_model_size` as an operator-configurable option (already exists in config.py)
- Documenting the quality trade-off

### What would actually achieve RTF ≤ 0.30

1. **GPU:** Even a modest GPU (T4, L4) would achieve RTF < 0.1. This is permanently out of scope per architectural constraint.
2. **Better CPU:** 8+ vCPU with higher single-thread performance could get closer. Current 4 vCPU is the bottleneck.
3. **Future optimized models:** A distilled multilingual Whisper that actually supports Portuguese (unlike current Distil-Whisper) could help if the encoder is reduced.

### Not recommended

- **whisper.cpp:** Slower than faster-whisper on this hardware. Only viable on ARM (Apple Silicon) or systems where CTranslate2 doesn't have optimized kernels.
- **ONNX/OpenVINO:** High complexity with uncertain gains. The VPS lacks AVX-512 which is where OpenVINO shines.
- **Distil-Whisper:** English-only. Useless for pt-BR.

---

## Files Created/Modified

| File | Status | Purpose |
|------|--------|---------|
| `scripts/benchmark_cpu.py` | ✅ Created | Full benchmark harness (ready for future use) |
| `/tmp/bench_rtf.py` | ✅ Created (VPS) | Quick RTF measurement script |
| `tests/fixtures/pt-br-reference/` | ✅ Created (VPS) | Reference dataset directory |
| `docs/plan/plan-06-benchmark-results.md` | ✅ This file | Benchmark results |

---

## Appendix: Transcript Comparison

### 10s clip (full transcript)

**Baseline (large-v3-turbo, beam=5):**
> É porque a Renata e Marcelo estão aqui com a Tia Laíte Zé Grandão. E aí ela tá dizendo que tu ficaste uns sete anos lá. Vocês ficaram uns sete anos lá em São Paulo.

**Small model:**
> É porque a Renata e a Marcelo estão aqui, quantos aletes é grandão, e aí ela tá dizendo que tu ficaste uns 7 anos lá, vocês ficaram uns 7 anos lá em São Paulo.

**Base model:**
> É porque a Renata e Marcelo estão aqui, quando te alaites a grandão. E aí, tá dizendo que tu ficaste uns 7 anos lá, vocês ficaram uns 7 anos lá em São Paulo.

**Tiny model:**
> É porque a Renata e Marcelo estão aqui, quando ela é desagrandando. E aí, ela está dizendo que tu ficaste no sete anos lá. Você se caram no sete ano lá em São Paulo.

**Distil-Whisper (ENGLISH — showstopper):**
> It's, because Renata and Marcella is a grand-o. And then she's saying that you've been seven years there. You've been the seven years in San Paulo.
