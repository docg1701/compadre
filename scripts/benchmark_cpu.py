#!/usr/bin/env python3
"""CPU STT benchmark harness — RTF and WER measurement across configurations.

Usage:
  uv run python scripts/benchmark_cpu.py --baseline
  uv run python scripts/benchmark_cpu.py --candidate distil-large-v3 --beam-size 1 --vad
  uv run python scripts/benchmark_cpu.py --all

Outputs:
  - Per-config RTF table (mean/median/P95 across durations)
  - WER table if reference transcripts are available
  - Recommendation against acceptance gates
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ClipResult:
    label: str
    audio_path: str
    audio_duration_s: float
    wall_time_s: float
    rtf: float
    num_segments: int
    transcript: str
    language: str
    language_probability: float


@dataclass
class ConfigResult:
    config_name: str
    model: str
    compute_type: str
    beam_size: int
    cpu_threads: int
    vad_filter: bool
    clips: list[ClipResult] = field(default_factory=list)
    wer_scores: dict[str, float] = field(default_factory=dict)

    @property
    def rtf_values(self) -> list[float]:
        return [c.rtf for c in self.clips]

    @property
    def mean_rtf(self) -> float:
        return statistics.mean(self.rtf_values)

    @property
    def median_rtf(self) -> float:
        return statistics.median(self.rtf_values)

    @property
    def p95_rtf(self) -> float:
        sorted_rtf = sorted(self.rtf_values)
        idx = int(len(sorted_rtf) * 0.95)
        return sorted_rtf[min(idx, len(sorted_rtf) - 1)]

    @property
    def mean_wer(self) -> float | None:
        if not self.wer_scores:
            return None
        return statistics.mean(self.wer_scores.values())


# ---------------------------------------------------------------------------
# Configuration presets
# ---------------------------------------------------------------------------


PRESETS: dict[str, dict[str, Any]] = {
    "baseline": {
        "model": "large-v3-turbo",
        "compute_type": "int8",
        "beam_size": 5,
        "cpu_threads": 4,
        "vad_filter": False,
    },
    "distil-large-v3": {
        "model": "distil-large-v3",
        "compute_type": "int8",
        "beam_size": 5,
        "cpu_threads": 4,
        "vad_filter": False,
    },
    "distil-v3-beam1": {
        "model": "distil-large-v3",
        "compute_type": "int8",
        "beam_size": 1,
        "cpu_threads": 4,
        "vad_filter": False,
    },
    "distil-v3-beam1-vad": {
        "model": "distil-large-v3",
        "compute_type": "int8",
        "beam_size": 1,
        "cpu_threads": 4,
        "vad_filter": True,
    },
    "greedy": {
        "model": "large-v3-turbo",
        "compute_type": "int8",
        "beam_size": 1,
        "cpu_threads": 4,
        "vad_filter": False,
    },
    "greedy-vad": {
        "model": "large-v3-turbo",
        "compute_type": "int8",
        "beam_size": 1,
        "cpu_threads": 4,
        "vad_filter": True,
    },
    "baseline-vad": {
        "model": "large-v3-turbo",
        "compute_type": "int8",
        "beam_size": 5,
        "cpu_threads": 4,
        "vad_filter": True,
    },
    "threads2": {
        "model": "large-v3-turbo",
        "compute_type": "int8",
        "beam_size": 5,
        "cpu_threads": 2,
        "vad_filter": False,
    },
    "threads8": {
        "model": "large-v3-turbo",
        "compute_type": "int8",
        "beam_size": 5,
        "cpu_threads": 8,
        "vad_filter": False,
    },
    "int8-fp16": {
        "model": "large-v3-turbo",
        "compute_type": "int8_float16",
        "beam_size": 5,
        "cpu_threads": 4,
        "vad_filter": False,
    },
    "fp16": {
        "model": "large-v3-turbo",
        "compute_type": "float16",
        "beam_size": 5,
        "cpu_threads": 4,
        "vad_filter": False,
    },
    "distil-v3-threads8": {
        "model": "distil-large-v3",
        "compute_type": "int8",
        "beam_size": 1,
        "cpu_threads": 8,
        "vad_filter": True,
    },
}


# ---------------------------------------------------------------------------
# Benchmark functions
# ---------------------------------------------------------------------------


def find_audio_files(audio_dir: str) -> dict[str, str]:
    """Find audio files and their reference transcripts.

    Returns dict mapping clip_id to audio_path.
    Also checks for {clip_id}.txt reference transcripts.
    """
    audio_dir = Path(audio_dir)
    clips: dict[str, str] = {}
    for ext in (".ogg", ".opus", ".wav", ".m4a", ".mp3"):
        for f in audio_dir.glob(f"*{ext}"):
            clip_id = f.stem
            clips[clip_id] = str(f)
    return clips


def load_reference_transcripts(audio_dir: str) -> dict[str, str]:
    """Load ground-truth transcripts from {clip_id}.txt files."""
    audio_dir = Path(audio_dir)
    refs: dict[str, str] = {}
    for txt_file in audio_dir.glob("*.txt"):
        clip_id = txt_file.stem
        refs[clip_id] = txt_file.read_text(encoding="utf-8").strip()
    return refs


def normalize_transcript(text: str) -> str:
    """Normalize transcript for WER comparison.

    Lowercase, strip punctuation, normalize whitespace.
    """
    import re

    text = text.lower()
    # Remove punctuation but keep letters, numbers, and spaces
    text = re.sub(r"[^\w\s]", "", text)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compute_wer(hypothesis: str, reference: str) -> float:
    """Compute Word Error Rate using jiwer."""
    from jiwer import wer

    hyp_norm = normalize_transcript(hypothesis)
    ref_norm = normalize_transcript(reference)
    return wer(ref_norm, hyp_norm)


def probe_duration(audio_path: str) -> float:
    """Get audio duration via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def run_benchmark(
    config_name: str,
    model_name: str,
    compute_type: str,
    beam_size: int,
    cpu_threads: int,
    vad_filter: bool,
    audio_files: dict[str, str],
    warmup_runs: int = 3,
    hf_home: str | None = None,
) -> ConfigResult:
    """Run a single benchmark configuration."""
    from faster_whisper import WhisperModel

    if hf_home:
        os.environ["HF_HOME"] = hf_home

    result = ConfigResult(
        config_name=config_name,
        model=model_name,
        compute_type=compute_type,
        beam_size=beam_size,
        cpu_threads=cpu_threads,
        vad_filter=vad_filter,
    )

    print(f"\n{'='*70}")
    print(f"  Config: {config_name}")
    print(f"  Model: {model_name}, compute={compute_type}, beam={beam_size}, "
          f"threads={cpu_threads}, vad={vad_filter}")
    print(f"{'='*70}")

    # Load model
    t0 = time.monotonic()
    print(f"  Loading model {model_name}...")
    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type=compute_type,
        cpu_threads=cpu_threads,
    )
    load_time = time.monotonic() - t0
    print(f"  Model loaded in {load_time:.1f}s")

    # Warm-up
    if warmup_runs > 0:
        print(f"  Warming up ({warmup_runs} runs)...")
        warmup_path = next(iter(audio_files.values()))
        for i in range(warmup_runs):
            segs, _ = model.transcribe(
                warmup_path,
                beam_size=beam_size,
                language="pt",
                condition_on_previous_text=False,
                word_timestamps=True,
                vad_filter=vad_filter,
            )
            list(segs)
            if i == 0:
                print(f"    Run 1/{warmup_runs} complete")
        print(f"  Warm-up complete ({warmup_runs} runs)")

    # Benchmark each clip
    for clip_id, audio_path in sorted(audio_files.items()):
        dur = probe_duration(audio_path)

        start = time.monotonic()
        segments, info = model.transcribe(
            audio_path,
            beam_size=beam_size,
            language="pt",
            condition_on_previous_text=False,
            word_timestamps=True,
            vad_filter=vad_filter,
        )
        segment_list = list(segments)
        elapsed = time.monotonic() - start

        transcript = " ".join(s.text for s in segment_list)
        rtf = elapsed / dur

        clip_result = ClipResult(
            label=clip_id,
            audio_path=audio_path,
            audio_duration_s=dur,
            wall_time_s=elapsed,
            rtf=rtf,
            num_segments=len(segment_list),
            transcript=transcript,
            language=info.language or "unknown",
            language_probability=info.language_probability,
        )
        result.clips.append(clip_result)

        print(
            f"    {clip_id:>16s} | {dur:5.1f}s -> {elapsed:5.1f}s | "
            f"RTF={rtf:.3f} | {len(segment_list)} segs | "
            f"lang={info.language}({info.language_probability:.2f})"
        )

    return result


def print_results_table(results: list[ConfigResult]) -> None:
    """Print a summary RTF table across all configs."""
    print(f"\n{'='*90}")
    print("  SUMMARY: RTF by Configuration")
    print(f"{'='*90}")
    header = (
        f"{'Config':<30s} | {'Mean RTF':>8s} | {'Median RTF':>10s} | "
        f"{'P95 RTF':>8s} | {'Clips':>5s}"
    )
    print(header)
    print("-" * len(header))

    # Sort by mean RTF (ascending, best first)
    for r in sorted(results, key=lambda x: x.mean_rtf):
        print(
            f"{r.config_name:<30s} | {r.mean_rtf:8.3f} | {r.median_rtf:10.3f} | "
            f"{r.p95_rtf:8.3f} | {len(r.clips):5d}"
        )

    # Print per-clip breakdown for top configs
    print(f"\n{'='*90}")
    print("  PER-CLIP RTF DETAIL")
    print(f"{'='*90}")
    for r in sorted(results, key=lambda x: x.mean_rtf):
        print(f"\n  [{r.config_name}] model={r.model}, beam={r.beam_size}, "
              f"threads={r.cpu_threads}, ctype={r.compute_type}, vad={r.vad_filter}")
        for c in r.clips:
            print(f"    {c.label:>16s}: {c.audio_duration_s:5.1f}s audio -> "
                  f"{c.wall_time_s:5.1f}s wall, RTF={c.rtf:.3f}")


def print_wer_table(results: list[ConfigResult]) -> None:
    """Print WER comparison table if reference transcripts available."""
    if not any(r.wer_scores for r in results):
        return

    print(f"\n{'='*90}")
    print("  WER COMPARISON (against reference transcripts)")
    print(f"{'='*90}")

    baseline_wer = None
    for r in results:
        if r.config_name == "baseline" and r.mean_wer is not None:
            baseline_wer = r.mean_wer
            break

    header = (
        f"{'Config':<30s} | {'Mean WER':>9s} | {'Δ vs baseline':>14s}"
    )
    print(header)
    print("-" * len(header))

    for r in results:
        if r.mean_wer is None:
            continue
        delta = ""
        if baseline_wer is not None and r.config_name != "baseline":
            delta_val = r.mean_wer - baseline_wer
            delta = f"+{delta_val:.1f} pp" if delta_val >= 0 else f"{delta_val:.1f} pp"
        print(f"{r.config_name:<30s} | {r.mean_wer:8.2f}% | {delta:>14s}")


def evaluate_gates(results: list[ConfigResult]) -> dict[str, str]:
    """Evaluate each config against acceptance gates.

    Returns dict mapping config_name to gate level (gold/silver/bronze/fail).
    """
    # Find baseline for WER regression calculation
    baseline = None
    for r in results:
        if r.config_name == "baseline":
            baseline = r
            break

    gates: dict[str, str] = {}
    for r in results:
        if r.config_name == "baseline":
            gates[r.config_name] = "(baseline)"
            continue

        # RTF for 10s clip (or closest)
        rtf = r.mean_rtf

        # WER delta
        wer_delta = 0.0
        if baseline and r.mean_wer is not None and baseline.mean_wer is not None:
            wer_delta = r.mean_wer - baseline.mean_wer

        if rtf <= 0.30 and wer_delta <= 2.0:
            gates[r.config_name] = "GOLD ⭐"
        elif rtf <= 0.40 and wer_delta <= 2.0:
            gates[r.config_name] = "SILVER 🥈"
        elif rtf <= 0.50 and wer_delta <= 3.0:
            gates[r.config_name] = "BRONZE 🥉"
        else:
            gates[r.config_name] = "FAIL ❌"

    return gates


def print_recommendation(
    results: list[ConfigResult], gates: dict[str, str]
) -> None:
    """Print final recommendation."""
    print(f"\n{'='*90}")
    print("  ACCEPTANCE GATES")
    print(f"{'='*90}")
    header = f"{'Config':<30s} | {'Gate':>10s} | {'Mean RTF':>8s} | {'Mean WER':>9s}"
    print(header)
    print("-" * len(header))

    for r in sorted(results, key=lambda x: x.mean_rtf):
        gate = gates.get(r.config_name, "?")
        wer_str = f"{r.mean_wer:.2f}%" if r.mean_wer else "N/A"
        print(f"{r.config_name:<30s} | {gate:>10s} | {r.mean_rtf:8.3f} | {wer_str:>9s}")

    # Find best
    best = None
    for r in sorted(results, key=lambda x: x.mean_rtf):
        gate = gates.get(r.config_name, "")
        if gate in ("GOLD ⭐", "SILVER 🥈", "BRONZE 🥉"):
            best = r
            break

    print(f"\n{'─'*90}")
    if best:
        print(f"  RECOMMENDATION: {best.config_name}")
        print(f"    Model: {best.model}, beam={best.beam_size}, "
              f"threads={best.cpu_threads}, compute={best.compute_type}, "
              f"vad={best.vad_filter}")
        print(f"    Mean RTF: {best.mean_rtf:.3f}")
        print(f"    Gate: {gates[best.config_name]}")
    else:
        print("  NO CONFIG PASSED ANY GATE")
        print("  Best realistic RTF:")
        for r in sorted(results, key=lambda x: x.mean_rtf)[:3]:
            print(f"    {r.config_name}: RTF={r.mean_rtf:.3f}")
    print(f"{'─'*90}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compadre CPU STT benchmark harness"
    )
    parser.add_argument(
        "--audio-dir",
        default="/tmp/benchmark_audio",
        help="Directory containing audio files for benchmarking",
    )
    parser.add_argument(
        "--reference-dir",
        default=None,
        help="Directory with reference transcripts ({clip_id}.txt files)",
    )
    parser.add_argument(
        "--hf-home",
        default=os.environ.get("HF_HOME", "/app/models"),
        help="HuggingFace cache directory",
    )
    parser.add_argument(
        "--preset",
        action="append",
        dest="presets",
        help="Run specific preset(s). Use multiple times for multiple presets.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all presets",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Run baseline only",
    )
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=3,
        help="Number of warm-up transcriptions before timing",
    )
    parser.add_argument(
        "--json-output",
        type=str,
        default=None,
        help="Write full results to JSON file",
    )

    args = parser.parse_args()

    # Determine which presets to run
    if args.presets:
        presets_to_run = args.presets
    elif args.all:
        presets_to_run = list(PRESETS.keys())
    elif args.baseline:
        presets_to_run = ["baseline"]
    else:
        # Default: run baseline + top candidates
        presets_to_run = [
            "baseline",
            "distil-large-v3",
            "distil-v3-beam1",
            "distil-v3-beam1-vad",
            "greedy",
            "greedy-vad",
            "baseline-vad",
            "threads2",
            "threads8",
            "int8-fp16",
            "fp16",
        ]

    # Find audio files
    audio_files = find_audio_files(args.audio_dir)
    if not audio_files:
        print(f"ERROR: No audio files found in {args.audio_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(audio_files)} audio file(s) in {args.audio_dir}:")
    for clip_id, path in sorted(audio_files.items()):
        dur = probe_duration(path)
        print(f"  {clip_id}: {path} ({dur:.1f}s)")

    # Load reference transcripts
    ref_dir = args.reference_dir or args.audio_dir
    references = load_reference_transcripts(ref_dir)
    if references:
        print(f"Found {len(references)} reference transcript(s)")
    else:
        print("No reference transcripts found — WER will not be computed")

    # Run benchmarks
    results: list[ConfigResult] = []
    for preset_name in presets_to_run:
        if preset_name not in PRESETS:
            print(f"WARNING: Unknown preset '{preset_name}', skipping", file=sys.stderr)
            continue
        cfg = PRESETS[preset_name]
        result = run_benchmark(
            config_name=preset_name,
            model_name=cfg["model"],
            compute_type=cfg["compute_type"],
            beam_size=cfg["beam_size"],
            cpu_threads=cfg["cpu_threads"],
            vad_filter=cfg["vad_filter"],
            audio_files=audio_files,
            warmup_runs=args.warmup_runs,
            hf_home=args.hf_home,
        )

        # Compute WER if references available
        if references:
            for clip_id, ref_text in references.items():
                # Find matching clip
                matching = [c for c in result.clips if c.label == clip_id]
                if matching:
                    clip = matching[0]
                    try:
                        wer_score = compute_wer(clip.transcript, ref_text)
                        result.wer_scores[clip_id] = wer_score
                    except Exception as exc:
                        print(f"    WER computation failed for {clip_id}: {exc}")

        results.append(result)

    # Print results
    print_results_table(results)
    print_wer_table(results)
    gates = evaluate_gates(results)
    print_recommendation(results, gates)

    # JSON output
    if args.json_output:
        output = {
            "results": [
                {
                    "config_name": r.config_name,
                    "model": r.model,
                    "compute_type": r.compute_type,
                    "beam_size": r.beam_size,
                    "cpu_threads": r.cpu_threads,
                    "vad_filter": r.vad_filter,
                    "mean_rtf": r.mean_rtf,
                    "median_rtf": r.median_rtf,
                    "p95_rtf": r.p95_rtf,
                    "mean_wer": r.mean_wer,
                    "wer_scores": r.wer_scores,
                    "clips": [
                        {
                            "label": c.label,
                            "audio_duration_s": c.audio_duration_s,
                            "wall_time_s": c.wall_time_s,
                            "rtf": c.rtf,
                            "num_segments": c.num_segments,
                            "language": c.language,
                        }
                        for c in r.clips
                    ],
                    "gate": gates.get(r.config_name, "?"),
                }
                for r in results
            ]
        }
        with open(args.json_output, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nFull results written to {args.json_output}")


if __name__ == "__main__":
    main()
