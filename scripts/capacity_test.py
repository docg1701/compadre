#!/usr/bin/env python3
"""Compadre STT capacity test — rate-based throughput measurement.

Sends transcription requests at controlled rates (audios/min) and measures
actual response times without artificial timeouts. Detects queue saturation
and produces a capacity report with cost comparison vs OpenAI Whisper API.

Usage:
    uv run --group test scripts/capacity_test.py \
        --url https://YOUR_VPS_DOMAIN \
        --api-key <key>

Default rates: 5, 10, 20, 30 req/min (override with --rates).
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
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Synthetic audio generation
# ---------------------------------------------------------------------------

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
        frequency: Sine wave frequency in Hz.
        amplitude: Amplitude 0.0-1.0.

    Returns:
        WAV file bytes for multipart/form-data upload.
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
# Pricing constants
# ---------------------------------------------------------------------------

# Hetzner CX33 (4 vCPU shared, 8 GB RAM) — April 2026
HETZNER_CX33_EUR = 6.49
EUR_TO_USD = 1.08

# OpenAI Whisper API (April 2026): $0.006 per minute of audio
OPENAI_WHISPER_USD_PER_MIN = 0.006

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RequestMetric:
    """Per-request measurement."""

    request_id: int
    tier_rate: int
    timestamp: float
    audio_duration_s: float
    status_code: int
    latency_s: float
    response_chars: int
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
class TierResult:
    """Aggregated results for one rate tier."""

    target_rate: int
    requests: list[RequestMetric] = field(default_factory=list)
    health: list[HealthSample] = field(default_factory=list)
    wall_time: float = 0.0


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

REQUEST_FIELDS = [
    "request_id",
    "tier_rate",
    "timestamp",
    "audio_duration_s",
    "status_code",
    "latency_s",
    "response_chars",
    "queue_depth",
    "memory_mb",
    "error",
]


def write_csv(path: Path, results: list[RequestMetric]) -> None:
    """Write all per-request metrics to a single CSV."""
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REQUEST_FIELDS)
        w.writeheader()
        for m in results:
            w.writerow(
                {
                    "request_id": m.request_id,
                    "tier_rate": m.tier_rate,
                    "timestamp": f"{m.timestamp:.3f}",
                    "audio_duration_s": f"{m.audio_duration_s:.2f}",
                    "status_code": m.status_code,
                    "latency_s": f"{m.latency_s:.3f}",
                    "response_chars": m.response_chars,
                    "queue_depth": m.queue_depth,
                    "memory_mb": f"{m.memory_mb:.0f}",
                    "error": m.error,
                }
            )


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def pct(data: list[float], p: float) -> float:
    """Linear-interpolated percentile."""
    if not data:
        return 0.0
    k = (len(data) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (k - lo) * (data[hi] - data[lo])


# ---------------------------------------------------------------------------
# Health monitor
# ---------------------------------------------------------------------------


async def health_monitor(
    client: httpx.AsyncClient,
    base_url: str,
    stop: asyncio.Event,
) -> tuple[int, float, list[HealthSample]]:
    """Poll /health every second; return latest (qd, mem, samples)."""
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
# Run a single rate tier
# ---------------------------------------------------------------------------


async def run_tier(
    target_rate: int,
    base_url: str,
    api_key: str,
    audio_duration_s: float,
    response_format: str,
    tier_duration_s: int,
    output_dir: Path,
    rng: random.Random,
    *,
    insecure: bool = False,
) -> TierResult:
    """Run one rate tier: send at target_rate req/min for tier_duration_s."""
    interval = 60.0 / target_rate
    num_requests = max(20, int(target_rate * tier_duration_s / 60))

    # Vary audio duration slightly per request for realism
    durations = [
        max(0.5, audio_duration_s * f)
        for f in [0.3, 0.5, 1.0, 0.7, 1.5, 0.4, 2.0, 0.6, 0.8, 1.2]
    ]
    audio_bufs = [generate_sine_wav_bytes(d) for d in durations]

    print(f"\n{'=' * 70}")
    print(
        f"  TIER: {target_rate} req/min"
        f" | interval={interval:.1f}s"
        f" | {num_requests} requests over {tier_duration_s}s"
    )
    print(f"{'=' * 70}")

    result = TierResult(target_rate=target_rate)
    stop = asyncio.Event()
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(verify=not insecure) as client:
        async def _health_loop() -> None:
            nonlocal live_qd, live_mem
            while not stop.is_set():
                try:
                    r = await client.get(f"{base_url}/health", timeout=5)
                    data = r.json()
                    live_qd = data.get("queue_depth", -1)
                    live_mem = data.get("memory_mb", -1)
                    result.health.append(
                        HealthSample(
                            timestamp=time.time(),
                            queue_depth=live_qd,
                            memory_mb=live_mem,
                        )
                    )
                except Exception:
                    pass
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)

        live_qd = 0
        live_mem = 0.0
        health_task = asyncio.create_task(_health_loop())

        async def _send(req_id: int) -> None:
            dur = durations[req_id % len(durations)]
            audio_bytes = audio_bufs[req_id % len(audio_bufs)]
            files = {
                "file": (
                    f"cap_{target_rate}_{req_id:04d}.wav",
                    audio_bytes,
                    "audio/wav",
                )
            }
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
                    timeout=600,
                )
                status = r.status_code
                resp_chars = len(r.text)
                if status != 200:
                    error = r.text[:200]
            except Exception as exc:
                error = str(exc)[:200]

            latency = time.time() - t0
            result.requests.append(
                RequestMetric(
                    request_id=req_id,
                    tier_rate=target_rate,
                    timestamp=t0,
                    audio_duration_s=dur,
                    status_code=status,
                    latency_s=latency,
                    response_chars=resp_chars,
                    queue_depth=live_qd,
                    memory_mb=live_mem,
                    error=error,
                )
            )

            ok_tag = (
                "\033[32mOK\033[0m"
                if status == 200
                else f"\033[31mFAIL({status})\033[0m"
            )
            print(
                f"  [{time.strftime('%H:%M:%S')}] "
                f"#{req_id:<3d} | {latency:6.1f}s | "
                f"dur={dur:.1f}s | {ok_tag} | "
                f"q:{live_qd} mem:{live_mem:.0f}MB"
            )

        t_start = time.time()
        pending: set[asyncio.Task[None]] = set()

        for i in range(num_requests):
            scheduled_at = t_start + i * interval
            delay = scheduled_at - time.time()
            if delay > 0:
                await asyncio.sleep(delay)

            task = asyncio.create_task(_send(i))
            pending.add(task)
            task.add_done_callback(pending.discard)

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        result.wall_time = time.time() - t_start
        stop.set()
        await health_task

    return result


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def build_report(
    tiers: list[TierResult],
    url: str,
    audio_duration_s: float,
    response_format: str,
    seed: int,
) -> str:
    """Generate a full Markdown capacity report."""
    lines: list[str] = []
    ln = lines.append

    ln("# Compadre STT Capacity Test Report")
    ln("")
    ln(f"- **Server:** {url}")
    ln(f"- **Audio duration:** {audio_duration_s:.1f}s (synthetic sine wave)")
    ln(f"- **Response format:** {response_format}")
    ln(f"- **Seed:** {seed}")
    ln(f"- **Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}")
    ln("- **VPS:** Hetzner CX33 — 4 vCPU shared, 8 GB RAM")
    ln("- **Engine:** faster-whisper large-v3-turbo (int8, CPU-only)")
    ln("")

    # --- Throughput table ---
    ln("## Throughput by Target Rate")
    ln("")
    ln("| Rate | Actual | Success | Mean | p50 | p95 | p99 | Max | Queue |")
    ln("|------|--------|---------|------|-----|-----|-----|-----|-------|")

    for t in tiers:
        ok = [m for m in t.requests if m.status_code == 200]
        total = len(t.requests)
        actual = len(ok) / t.wall_time * 60 if t.wall_time else 0
        lats = sorted(m.latency_s for m in ok) if ok else [0.0]
        qds = [s.queue_depth for s in t.health if s.queue_depth >= 0]
        max_q = max(qds) if qds else 0

        ln(
            f"| {t.target_rate:>3d}/min"
            f" | {actual:>5.1f}/min"
            f" | {len(ok)}/{total}"
            f" | {statistics.mean(lats):>5.1f}s"
            f" | {pct(lats, 50):>5.1f}s"
            f" | {pct(lats, 95):>5.1f}s"
            f" | {pct(lats, 99):>5.1f}s"
            f" | {max(lats):>5.1f}s"
            f" | {max_q:>5d}"
            f" |"
        )

    # --- Error breakdown ---
    errors_found = False
    for t in tiers:
        if any(m.status_code != 200 for m in t.requests):
            errors_found = True
            break
    if errors_found:
        ln("")
        ln("## Errors by Rate Tier")
        ln("")
        ln("| Rate | Error | Count |")
        ln("|------|-------|-------|")
        for t in tiers:
            error_counts: dict[str, int] = {}
            for m in t.requests:
                if m.status_code != 200:
                    err_label = (
                        f"HTTP {m.status_code}"
                        if not m.error
                        else f"{m.status_code}: {m.error[:60]}"
                    )
                    error_counts[err_label] = error_counts.get(err_label, 0) + 1
            for err, cnt in sorted(error_counts.items(), key=lambda x: -x[1]):
                ln(f"| {t.target_rate}/min | {err} | {cnt} |")

    # --- Sustainable throughput ---
    ln("")
    ln("## Sustainable Throughput")
    ln("")
    ln("Criteria: **<5% error rate** AND **p95 latency < 30s**.")
    ln("")

    sustainable: TierResult | None = None
    for t in tiers:
        ok = [m for m in t.requests if m.status_code == 200]
        total = len(t.requests)
        error_rate = (total - len(ok)) / total * 100 if total else 0
        ok_lats = sorted(m.latency_s for m in ok) if ok else [0.0]
        p95_val = pct(ok_lats, 95) if ok_lats else 0.0
        if error_rate < 5 and p95_val < 30:
            sustainable = t
        else:
            break

    if sustainable is None:
        ln(
            "**No tier met the criteria.**"
            " The system may be overloaded at all tested rates."
        )
        ln("")
        capacity_rpm = 5
    else:
        ok = [m for m in sustainable.requests if m.status_code == 200]
        capacity_rpm = (
            len(ok) / sustainable.wall_time * 60
            if sustainable.wall_time
            else 10
        )
        ln(
            f"- **Sustainable rate:** {sustainable.target_rate} req/min"
            f" ({capacity_rpm:.1f} actual)"
        )
        ln(
            f"- **p95 latency at this rate:**"
            f" {pct(sorted(m.latency_s for m in ok), 95):.1f}s"
        )
        err_pct = (
            (len(sustainable.requests) - len(ok))
            / len(sustainable.requests) * 100
        )
        ln(f"- **Error rate:** {err_pct:.1f}%")
        ln("")

    # --- Scaling recommendations ---
    ln("## Scaling Recommendations")
    ln("")
    ln(
        "Compadre uses a single FIFO lock per replica."
        f" Each replica handles ~{capacity_rpm:.0f} req/min sustainably."
    )
    ln("Scale horizontally by increasing `deploy.replicas` in `compose.yaml`.")
    ln("")
    ln("| Target Throughput | Replicas | VPS Monthly Cost |")
    ln("|-------------------|----------|------------------|")

    for target_rpm in [10, 30, 60, 120, 300, 600]:
        reps = max(1, -(-target_rpm // max(1, int(capacity_rpm))))
        cost = reps * HETZNER_CX33_EUR
        ln(
            f"| {target_rpm} req/min"
            f" | {reps}x CX33"
            f" | €{cost:.2f} (~${cost * EUR_TO_USD:.2f}) |"
        )

    # --- Cost comparison: Compadre vs OpenAI Whisper API ---
    ln("")
    ln("## Cost Comparison: Compadre vs OpenAI Whisper API")
    ln("")
    ln(
        f"Assumptions: avg audio = {audio_duration_s:.0f}s"
        f" ({(audio_duration_s / 60):.2f} min) per transcription."
    )
    ln("")
    ln(
        f"- **OpenAI Whisper API:** ${OPENAI_WHISPER_USD_PER_MIN:.3f}/min"
        f" — pay-per-use, rounded to nearest second"
    )
    ln(
        f"- **Compadre (1x CX33):** €{HETZNER_CX33_EUR:.2f}/mo"
        f" (~${HETZNER_CX33_EUR * EUR_TO_USD:.2f}/mo)"
        f" — unlimited transcriptions"
    )
    ln("")

    ln("| Monthly Usage | Audio Minutes | Compadre | OpenAI Whisper | Savings |")
    ln("|---------------|---------------|----------|----------------|---------|")

    usage_levels = [
        ("500 msgs", 500),
        ("2,000 msgs", 2000),
        ("5,000 msgs", 5000),
        ("10,000 msgs", 10000),
        ("30,000 msgs", 30000),
        ("100,000 msgs", 100000),
    ]

    comadre_cost_usd = HETZNER_CX33_EUR * EUR_TO_USD

    for label, msg_count in usage_levels:
        audio_min = msg_count * audio_duration_s / 60
        openai_cost = audio_min * OPENAI_WHISPER_USD_PER_MIN
        daily_cap = capacity_rpm * 60 * 24 * 30 * 0.3
        reps_needed = max(1, -(-int(msg_count / daily_cap) // 1))
        compadre_scaled = reps_needed * comadre_cost_usd
        saving = (1 - compadre_scaled / openai_cost) * 100 if openai_cost > 0 else 0
        ln(
            f"| {label}"
            f" | {audio_min:,.0f}"
            f" | ${compadre_scaled:.2f} ({reps_needed}x CX33)"
            f" | ${openai_cost:.2f}"
            f" | {saving:+.0f}% |"
        )

    ln("")
    ln("### Break-even Analysis")
    ln("")
    per_min_cost = audio_duration_s / 60 * OPENAI_WHISPER_USD_PER_MIN
    break_even_msgs = comadre_cost_usd / per_min_cost
    ln(
        f"- **Break-even point:** ~{break_even_msgs:,.0f} msgs/mo"
        f" — above this, Compadre is cheaper"
    )
    ln(
        f"- At {break_even_msgs / 30:,.0f} msgs/day"
        f" ({break_even_msgs / 30 / 24:.0f} msg/h avg),"
        f" Compadre matches OpenAI Whisper pricing"
    )
    ln(
        "- For a 1,000-user WhatsApp bot"
        " (avg 3 audios/user/day x 30 days = 90,000 msgs/mo):"
    )
    monthly_90k = 90000 * audio_duration_s / 60 * OPENAI_WHISPER_USD_PER_MIN
    savings_pct = (1 - comadre_cost_usd / monthly_90k) * 100
    ln(
        f"  - OpenAI Whisper: ${monthly_90k:.0f}/mo"
    )
    ln(
        f"  - Compadre 1x CX33: ${comadre_cost_usd:.0f}/mo"
    )
    ln(
        f"  - Savings: ${monthly_90k - comadre_cost_usd:.0f}/mo"
        f" ({savings_pct:.0f}%)"
    )
    ln("")
    ln("### Key Takeaways")
    ln("")
    ln(f"- **Compadre fixed cost:** ${comadre_cost_usd:.2f}/mo (Hetzner CX33)")
    ln(
        "- **OpenAI Whisper scales linearly:**"
        " $0.006/min x minutes processed"
    )
    ln("- **Data privacy advantage:** Compadre processes audio on your own hardware")
    ln("- **No rate limits:** Self-hosted, no API quota to exhaust")
    ln(
        "- **Latency predictability:** No cross-region API call;"
        " latency depends only on your VPS specs"
    )
    ln("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description="Compadre STT capacity test")
    parser.add_argument(
        "--url",
        required=True,
        help="Base URL (e.g. https://YOUR_VPS_DOMAIN)",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="API key for Authorization header",
    )
    parser.add_argument(
        "--rates",
        default="5,10,20,30",
        help="Comma-separated target rates in req/min (default: 5,10,20,30)",
    )
    parser.add_argument(
        "--duration-per-rate",
        type=int,
        default=120,
        help="Duration per rate tier in seconds (default: 120)",
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
        default="tests/results/capacity",
        help="Output directory (default: tests/results/capacity)",
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

    rates = [int(x.strip()) for x in args.rates.split(",")]
    for r in rates:
        if r < 1:
            print(f"ERROR: Rate must be >= 1, got {r}", file=sys.stderr)
            sys.exit(1)

    print(f"Compadre STT Capacity Test — {args.url}")
    print(f"Rates: {rates} req/min | Duration per rate: {args.duration_per_rate}s")
    print(
        f"Audio: {args.audio_duration}s"
        f" | Format: {args.response_format}"
    )
    print(f"Seed: {args.seed} | Output: {output_dir.resolve()}")

    # Quick connectivity check
    async with httpx.AsyncClient(verify=not args.insecure) as client:
        try:
            r = await client.get(f"{args.url}/health", timeout=10)
            h = r.json()
            print(f"Server: {h.get('status')} | version: {h.get('version')}")
        except Exception as exc:
            print(f"ERROR: Cannot reach {args.url}/health — {exc}", file=sys.stderr)
            sys.exit(1)

    all_results: list[TierResult] = []
    all_metrics: list[RequestMetric] = []

    for rate in rates:
        res = await run_tier(
            rate,
            args.url,
            args.api_key,
            args.audio_duration,
            args.response_format,
            args.duration_per_rate,
            output_dir,
            rng,
            insecure=args.insecure,
        )
        all_results.append(res)
        all_metrics.extend(res.requests)

    # Write CSV
    write_csv(output_dir / "all_requests.csv", all_metrics)

    # Build and write report
    report = build_report(
        all_results,
        args.url,
        args.audio_duration,
        args.response_format,
        args.seed,
    )
    report_path = output_dir / "capacity_report.md"
    report_path.write_text(report)

    print(f"\n{report}")
    print(f"\n{'=' * 70}")
    print(f"  Report: {report_path.resolve()}")
    print(f"  CSV:    {output_dir.resolve()}/all_requests.csv")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    asyncio.run(main())
