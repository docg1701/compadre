# pt-BR Reference Audio Dataset

## Purpose

Reference audio clips for Brazilian Portuguese STT benchmarking. Used by `scripts/benchmark_cpu.py` to measure Word Error Rate (WER) against ground-truth transcripts.

## Files

Each clip has two files:
- `{clip_id}.opus` — WhatsApp voice message in Opus format
- `{clip_id}.txt` — ground-truth human transcript (lowercase, no punctuation, normalized whitespace)

## Current Status

This directory exists on the VPS at `~/compadre/tests/fixtures/pt-br-reference/` but currently contains only one reference clip (`audio-de-teste-01.ogg`) without a human-verified ground-truth transcript.

To complete the dataset:
1. Record 10-20 WhatsApp voice messages from consenting native pt-BR speakers
2. Manually transcribe each to establish ground truth
3. Normalize: lowercase, strip punctuation, normalize whitespace
4. Save as `{clip_id}.opus` + `{clip_id}.txt`

## Usage

```bash
uv run python scripts/benchmark_cpu.py \
  --audio-dir tests/fixtures/pt-br-reference \
  --reference-dir tests/fixtures/pt-br-reference \
  --preset baseline --preset small
```

## Notes

- For the Plan 06 benchmarks, WER was not computed because ground-truth transcripts were unavailable. Instead, relative transcript quality was evaluated manually by comparing candidate outputs to the baseline model's output.
- The benchmark harness supports automatic WER computation via `jiwer` when reference transcripts are present.
