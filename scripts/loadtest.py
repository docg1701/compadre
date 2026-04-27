#!/usr/bin/env python3
"""Compadre STT load/saturation test.

Measures transcription latency, throughput, queue behavior, and error rates
under different workload patterns against a live Compadre instance.

Generates synthetic audio (sine wave) in-memory — no external audio files needed.

Usage:
    uv run --group test scripts/loadtest.py \
        --url https://YOUR_VPS_DOMAIN \
        --api-key <key> \
        --scenario all
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import math
import random
import statistics
import struct
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Synthetic audio generation
# ---------------------------------------------------------------------------

# 16 kHz mono PCM s16le — matches Compadre's preprocessing target
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
NUM_CHANNELS = 1


def generate_sine_wav_bytes(
    duration_s: float,
    frequency: float = 220.0,
    amplitude: float = 0.3,
) -> bytes:
    """Return a WAV file (16 kHz mono 16-bit PCM) as raw bytes.

    Args:
        duration_s: Audio duration in seconds.
        frequency: Sine wave frequency in Hz (default 220 Hz, A3).
        amplitude: Amplitude 0.0-1.0 (default 0.3, quiet but not silent).

    Returns:
        WAV file bytes, ready to send as multipart/form-data.
    """
    n_samples = int(duration_s * SAMPLE_RATE)
    buf = io.BytesIO()
    with wave.open(buf, "w") as wf:  # type: ignore[attr-defined]
        wf.setnchannels(NUM_CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        max_val = int(32767 * amplitude)
        for i in range(n_samples):
            sample = int(max_val * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE))
            wf.writeframes(struct.pack("<h", sample))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RequestMetric:
    """Per-request measurement."""

    request_id: int
    timestamp: float
    audio_duration_s: float
    status_code: int
    latency_s: float
    response_chars: int
    response_format: str
    queue_depth: int
    memory_mb: float
    error: str = ""


@dataclass
class HealthSample:
    """Periodic snapshot from /health endpoint."""

    timestamp: float
    queue_depth: int
    memory_mb: float


@dataclass
class ScenarioConfig:
    """Parameters for one test scenario."""

    name: str
    concurrency: int = 1
    duration_seconds: int = 0
    request_count: int = 0
    rate_per_minute: float = 0.0
    description: str = ""


def build_scenarios(
    concurrency: int,
    duration: int,
    rpm: float,
) -> list[ScenarioConfig]:
    """Build scenario list from CLI args.

    The --scenario flag picks one or more named scenarios:
      sequential  — one-at-a-time, 20 requests
      concurrent  — N simultaneous workers for M seconds
      sustained   — fixed rate for M seconds
    """
    all_scenarios: dict[str, ScenarioConfig] = {
        "sequential": ScenarioConfig(
            name="sequential",
            concurrency=1,
            request_count=20,
            description="Single-request latency across varied audio durations",
        ),
        "concurrent": ScenarioConfig(
            name="concurrent",
            concurrency=concurrency,
            duration_seconds=duration,
            description=f"{concurrency}-worker concurrent load for {duration}s",
        ),
        "sustained": ScenarioConfig(
            name="sustained",
            concurrency=1,
            duration_seconds=duration,
            rate_per_minute=rpm,
            description=f"Sustained {rpm:.0f} req/min for {duration}s",
        ),
    }
    return list(all_scenarios.values())


# ---------------------------------------------------------------------------
# Health monitor
# ---------------------------------------------------------------------------


async def health_monitor(
    client: httpx.AsyncClient,
    base_url: str,
    stop: asyncio.Event,
) -> tuple[int, float, list[HealthSample]]:
    """Poll /health every second; return latest (queue_depth, memory_mb, samples)."""
    samples: list[HealthSample] = []
    qd = 0
    mem = 0.0
    while not stop.is_set():
        try:
            r = await client.get(f"{base_url}/health", timeout=5)
            data = r.json()
            qd = data.get("queue_depth", -1)
            mem = data.get("memory_mb", -1)
            samples.append(
                HealthSample(timestamp=time.time(), queue_depth=qd, memory_mb=mem)
            )
        except Exception:
            pass
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.0)
    return qd, mem, samples


# ---------------------------------------------------------------------------
# Single request
# ---------------------------------------------------------------------------


async def send_one(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    audio_bytes: bytes,
    audio_duration_s: float,
    response_format: str,
    req_id: int,
    live_qd: int,
    live_mem: float,
) -> RequestMetric:
    """Send one transcription request; return metric."""
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": (f"test_{req_id:04d}.wav", audio_bytes, "audio/wav")}
    data = {"response_format": response_format}

    t0 = time.time()
    status = 0
    resp_chars = 0
    error = ""

    try:
        r = await client.post(
            f"{base_url}/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
            timeout=130,
        )
        status = r.status_code
        resp_chars = len(r.text)
        if status != 200:
            error = r.text[:200]
    except httpx.TimeoutException:
        error = "timeout"
    except Exception as exc:
        error = str(exc)[:200]

    latency = time.time() - t0
    return RequestMetric(
        request_id=req_id,
        timestamp=t0,
        audio_duration_s=audio_duration_s,
        status_code=status,
        latency_s=latency,
        response_chars=resp_chars,
        response_format=response_format,
        queue_depth=live_qd,
        memory_mb=live_mem,
        error=error,
    )


# ---------------------------------------------------------------------------
# Sequential worker
# ---------------------------------------------------------------------------


async def run_sequential(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    config: ScenarioConfig,
    audio_duration: float,
    response_format: str,
    live_qd: int,
    live_mem: float,
    stop: asyncio.Event,
    rng: random.Random,
) -> list[RequestMetric]:
    """Send requests one at a time, varying audio duration."""
    results: list[RequestMetric] = []
    durations = [
        max(0.5, audio_duration * f)
        for f in [0.2, 0.5, 1.0, 0.3, 0.7, 2.0, 0.4, 1.5, 0.6, 0.8]
        for _ in range(2)
    ][: config.request_count]
    rng.shuffle(durations)

    for i, dur in enumerate(durations):
        if stop.is_set():
            break
        # Generate fresh audio bytes per request (in-memory, fast)
        audio_bytes = generate_sine_wav_bytes(dur)
        metric = await send_one(
            client,
            base_url,
            api_key,
            audio_bytes,
            dur,
            response_format,
            i,
            live_qd,
            live_mem,
        )
        results.append(metric)
        ok_tag = (
            "\033[32mOK\033[0m"
            if metric.status_code == 200
            else f"\033[31mFAIL({metric.status_code})\033[0m"
        )
        print(
            f"  [{time.strftime('%H:%M:%S')}] "
            f"req #{metric.request_id:<4d} | {metric.status_code} | "
            f"{metric.latency_s:5.1f}s | {metric.response_chars:4d} chars | "
            f"audio {dur:.1f}s | {ok_tag}"
        )
    return results


# ---------------------------------------------------------------------------
# Concurrent worker
# ---------------------------------------------------------------------------


async def concurrent_worker(
    worker_id: int,
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    audio_bytes: bytes,
    audio_duration_s: float,
    response_format: str,
    counter: dict[str, int],
    lock: asyncio.Lock,
    results: list[RequestMetric],
    stop: asyncio.Event,
    live_qd: int,
    live_mem: float,
) -> None:
    """Worker coroutine for concurrent scenario."""
    while not stop.is_set():
        async with lock:
            req_id = counter["n"]
            counter["n"] += 1

        metric = await send_one(
            client,
            base_url,
            api_key,
            audio_bytes,
            audio_duration_s,
            response_format,
            req_id,
            live_qd,
            live_mem,
        )
        results.append(metric)
        ok_tag = (
            "\033[32mOK\033[0m"
            if metric.status_code == 200
            else f"\033[31mFAIL({metric.status_code})\033[0m"
        )
        print(
            f"  [{time.strftime('%H:%M:%S')}] "
            f"w{worker_id} req #{metric.request_id:<4d} | "
            f"{metric.status_code} | "
            f"{metric.latency_s:5.1f}s | {metric.response_chars:4d} chars | "
            f"{ok_tag}"
        )


# ---------------------------------------------------------------------------
# Sustained worker (rate-controlled)
# ---------------------------------------------------------------------------


async def sustained_worker(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    config: ScenarioConfig,
    audio_bytes: bytes,
    audio_duration_s: float,
    response_format: str,
    counter: dict[str, int],
    lock: asyncio.Lock,
    results: list[RequestMetric],
    stop: asyncio.Event,
    live_qd: int,
    live_mem: float,
) -> None:
    """Send requests at a fixed rate (req/min)."""
    interval = 60.0 / config.rate_per_minute
    t_start = time.time()

    while not stop.is_set():
        async with lock:
            req_id = counter["n"]
            counter["n"] += 1

        scheduled_at = t_start + req_id * interval
        delay = scheduled_at - time.time()
        if delay > 0:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
        if stop.is_set():
            break

        metric = await send_one(
            client,
            base_url,
            api_key,
            audio_bytes,
            audio_duration_s,
            response_format,
            req_id,
            live_qd,
            live_mem,
        )
        results.append(metric)
        ok_tag = (
            "\033[32mOK\033[0m"
            if metric.status_code == 200
            else f"\033[31mFAIL({metric.status_code})\033[0m"
        )
        print(
            f"  [{time.strftime('%H:%M:%S')}] "
            f"req #{metric.request_id:<4d} | {metric.status_code} | "
            f"{metric.latency_s:5.1f}s | {metric.response_chars:4d} chars | "
            f"{ok_tag}"
        )


# ---------------------------------------------------------------------------
# Run a single scenario
# ---------------------------------------------------------------------------


async def run_scenario(
    config: ScenarioConfig,
    base_url: str,
    api_key: str,
    audio_duration: float,
    response_format: str,
    output_dir: Path,
    rng: random.Random,
    *,
    insecure: bool = False,
) -> tuple[list[RequestMetric], list[HealthSample], float]:
    """Execute one scenario; return (metrics, health_samples, wall_time)."""
    print(f"\n{'=' * 70}")
    print(f"  SCENARIO: {config.name} — {config.description}")
    if config.rate_per_minute:
        print(f"  Rate: {config.rate_per_minute:.0f} req/min")
    print(f"{'=' * 70}")

    results: list[RequestMetric] = []
    stop = asyncio.Event()
    counter: dict[str, int] = {"n": 0}
    lock = asyncio.Lock()

    async with httpx.AsyncClient(verify=not insecure) as client:
        # Start health monitor
        health_task = asyncio.create_task(health_monitor(client, base_url, stop))

        # Pre-generate a shared audio buffer for concurrent/sustained
        audio_bytes = generate_sine_wav_bytes(audio_duration)

        t_start = time.time()

        if config.name == "sequential":
            # Sequential: one at a time via sequential worker
            seq_results = await run_sequential(
                client,
                base_url,
                api_key,
                config,
                audio_duration,
                response_format,
                0,
                0.0,
                stop,
                rng,
            )
            results = seq_results
        elif config.rate_per_minute > 0:
            # Sustained: rate-controlled single worker
            await sustained_worker(
                client,
                base_url,
                api_key,
                config,
                audio_bytes,
                audio_duration,
                response_format,
                counter,
                lock,
                results,
                stop,
                0,
                0.0,
            )
        else:
            # Concurrent: N workers simultaneously
            workers = [
                asyncio.create_task(
                    concurrent_worker(
                        i,
                        client,
                        base_url,
                        api_key,
                        audio_bytes,
                        audio_duration,
                        response_format,
                        counter,
                        lock,
                        results,
                        stop,
                        0,
                        0.0,
                    )
                )
                for i in range(config.concurrency)
            ]

            if config.duration_seconds:
                await asyncio.sleep(config.duration_seconds)
                stop.set()

            await asyncio.gather(*workers, return_exceptions=True)

        if not stop.is_set():
            stop.set()
        _, _, health_samples = await health_task

        wall_time = time.time() - t_start

    # Health samples are returned by the monitor task
    return results, health_samples, wall_time


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

REQUEST_FIELDS = [
    "request_id",
    "timestamp",
    "audio_duration_s",
    "status_code",
    "latency_s",
    "response_chars",
    "response_format",
    "queue_depth",
    "memory_mb",
    "error",
]

HEALTH_FIELDS = ["timestamp", "queue_depth", "memory_mb"]


def write_request_csv(path: Path, metrics: list[RequestMetric]) -> None:
    """Write per-request metrics to CSV."""
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REQUEST_FIELDS)
        w.writeheader()
        for m in metrics:
            w.writerow(
                {
                    "request_id": m.request_id,
                    "timestamp": f"{m.timestamp:.3f}",
                    "audio_duration_s": f"{m.audio_duration_s:.2f}",
                    "status_code": m.status_code,
                    "latency_s": f"{m.latency_s:.3f}",
                    "response_chars": m.response_chars,
                    "response_format": m.response_format,
                    "queue_depth": m.queue_depth,
                    "memory_mb": f"{m.memory_mb:.0f}",
                    "error": m.error,
                }
            )


def write_health_csv(path: Path, samples: list[HealthSample]) -> None:
    """Write health samples to CSV."""
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEALTH_FIELDS)
        w.writeheader()
        for s in samples:
            w.writerow(
                {
                    "timestamp": f"{s.timestamp:.3f}",
                    "queue_depth": s.queue_depth,
                    "memory_mb": str(int(s.memory_mb)) if s.memory_mb > 0 else "",
                }
            )


# ---------------------------------------------------------------------------
# Summary / statistics
# ---------------------------------------------------------------------------


def percentile(data: list[float], pct: float) -> float:
    """Linear-interpolated percentile."""
    if not data:
        return 0.0
    k = (len(data) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (k - lo) * (data[hi] - data[lo])


def summarize(
    name: str,
    description: str,
    metrics: list[RequestMetric],
    health_samples: list[HealthSample],
    wall_time: float,
) -> str:
    """Build a human-readable summary block for one scenario."""
    total = len(metrics)
    if total == 0:
        return f"## Scenario: {name}\nNo requests completed.\n"

    ok = [m for m in metrics if m.status_code == 200]
    failed = total - len(ok)
    ok_rate = len(ok) / total * 100 if total else 0
    throughput = total / wall_time * 60 if wall_time else 0
    success_throughput = len(ok) / wall_time * 60 if wall_time else 0

    latencies = sorted(m.latency_s for m in metrics)
    ok_latencies = sorted(m.latency_s for m in ok)

    qd = [s.queue_depth for s in health_samples if s.queue_depth >= 0]
    mem = [s.memory_mb for s in health_samples if s.memory_mb > 0]

    lines = [
        f"## Scenario: {name}",
        f"  {description}",
        "",
        f"  Wall time:           {wall_time:8.1f}s",
        f"  Requests total:      {total:8d}",
        f"  Succeeded (200):     {len(ok):8d} ({ok_rate:.1f}%)",
        f"  Failed:              {failed:8d} ({100 - ok_rate:.1f}%)",
        (
            f"  Throughput:          {throughput:8.1f} req/min"
            f" ({success_throughput:.1f} OK/min)"
        ),
        "",
        "  Latency (all requests):",
        f"    mean: {statistics.mean(latencies):8.2f}s",
        f"    p50:  {percentile(latencies, 50):8.2f}s",
        f"    p95:  {percentile(latencies, 95):8.2f}s",
        f"    p99:  {percentile(latencies, 99):8.2f}s",
        f"    max:  {max(latencies):8.2f}s",
    ]
    if ok_latencies and len(ok_latencies) != len(latencies):
        lines.extend(
            [
                "  Latency (200 OK only):",
                f"    mean: {statistics.mean(ok_latencies):8.2f}s",
                f"    p95:  {percentile(ok_latencies, 95):8.2f}s",
            ]
        )
    lines.append("")
    if qd:
        lines.append(
            f"  Queue depth:         avg={statistics.mean(qd):.1f}  max={max(qd)}"
        )
    if mem:
        lines.append(
            f"  Memory (MB):         start={mem[0]:.0f}"
            f"  peak={max(mem):.0f}  end={mem[-1]:.0f}"
        )
    lines.append("")

    if failed:
        error_counts: dict[str, int] = {}
        for m in metrics:
            if m.status_code != 200:
                key = (
                    f"{m.status_code}: {m.error[:80]}"
                    if m.error
                    else str(m.status_code)
                )
                error_counts[key] = error_counts.get(key, 0) + 1
        lines.append("  Errors:")
        for err, cnt in sorted(error_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {cnt:4d}x  {err}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description="Compadre STT load test")
    parser.add_argument(
        "--url", required=True, help="Base URL (e.g. https://YOUR_VPS_DOMAIN)"
    )
    parser.add_argument(
        "--api-key", required=True, help="API key for Authorization header"
    )
    parser.add_argument(
        "--scenario",
        default="all",
        help="Scenarios to run: sequential, concurrent, sustained, all (default: all)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Workers for concurrent scenario (default: 10)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=120,
        help="Duration in seconds for concurrent/sustained (default: 120)",
    )
    parser.add_argument(
        "--rpm",
        type=float,
        default=10.0,
        help="Requests per minute for sustained scenario (default: 10)",
    )
    parser.add_argument(
        "--audio-duration",
        type=float,
        default=3.0,
        help="Synthetic audio length in seconds (default: 3.0)",
    )
    parser.add_argument(
        "--response-format",
        default="text",
        choices=["text", "json", "verbose_json", "srt", "vtt"],
        help="Response format from STT API (default: text)",
    )
    parser.add_argument(
        "--output",
        default="tests/results/loadtest",
        help="Output directory (default: tests/results/loadtest)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (like curl -k)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible workload (default: 42)",
    )
    args = parser.parse_args()
    rng = random.Random(args.seed)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.scenario == "all":
        scenario_names = ["sequential", "concurrent", "sustained"]
    else:
        scenario_names = [s.strip() for s in args.scenario.split(",")]
        for name in scenario_names:
            if name not in ("sequential", "concurrent", "sustained"):
                print(
                    f"ERROR: Unknown scenario '{name}'. "
                    f"Options: sequential, concurrent, sustained, all",
                    file=sys.stderr,
                )
                sys.exit(1)

    configs = build_scenarios(args.concurrency, args.duration, args.rpm)
    config_map = {c.name: c for c in configs}
    to_run = [config_map[name] for name in scenario_names]

    print(f"Compadre STT Load Test — {args.url}")
    print(f"Scenarios: {', '.join(c.name for c in to_run)}")
    print(f"Audio: {args.audio_duration}s | Format: {args.response_format}")
    print(f"Seed: {args.seed} | Output: {output_dir.resolve()}")

    # Quick connectivity check
    async with httpx.AsyncClient(verify=not args.insecure) as client:
        try:
            r = await client.get(f"{args.url}/health", timeout=10)
            health = r.json()
            print(f"Server: {health.get('status')} | version: {health.get('version')}")
        except Exception as exc:
            print(f"ERROR: Cannot reach {args.url}/health — {exc}", file=sys.stderr)
            sys.exit(1)

    all_summaries: list[str] = []

    for config in to_run:
        metrics, health_samples, wall_time = await run_scenario(
            config,
            args.url,
            args.api_key,
            args.audio_duration,
            args.response_format,
            output_dir,
            rng,
            insecure=args.insecure,
        )

        # Write CSVs
        write_request_csv(output_dir / f"{config.name}_requests.csv", metrics)
        write_health_csv(output_dir / f"{config.name}_health.csv", health_samples)

        summary = summarize(
            config.name,
            config.description,
            metrics,
            health_samples,
            wall_time,
        )
        all_summaries.append(summary)
        print(f"\n{summary}")

    # Full report
    full_report = "# Compadre STT Load Test Results\n\n"
    full_report += f"- **URL:** {args.url}\n"
    full_report += f"- **Audio duration:** {args.audio_duration}s\n"
    full_report += f"- **Response format:** {args.response_format}\n"
    full_report += f"- **Seed:** {args.seed}\n"
    full_report += f"- **Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    full_report += "\n".join(all_summaries)

    summary_path = output_dir / "summary.md"
    summary_path.write_text(full_report)
    print(f"\n{'=' * 70}")
    print(f"  Full report saved to: {summary_path.resolve()}")
    print(f"  CSV data in: {output_dir.resolve()}/")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
