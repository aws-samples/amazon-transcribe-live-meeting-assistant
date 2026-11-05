# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""ASR benchmark: play one recording through each model configuration and compare cost and quality.

For each requested MicroVM bundle the stack is switched to that bundle, the WAV is streamed once
at real time over the transcriber WebSocket, and the run is measured from the transcriber's and
the engine's logs. An Amazon Transcribe run needs no switch. The original bundle is restored at
the end.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
from rich.console import Console

from lma_load.asr_benchmark import bundles as bundles_mod
from lma_load.asr_benchmark import measure, quality
from lma_load.auth.cognito import SyntheticUserPool
from lma_load.drivers.ws_streaming_driver import _load_wav, _stream_one
from lma_load.run_context import RunContext
from lma_load.stack_info import LMAStackInfo

logger = logging.getLogger(__name__)
console = Console()

SWITCH_MINUTES_ESTIMATE = 25
FALLBACK_RETRY_WAIT_S = 120
FELL_BACK = "the meeting fell back to Amazon Transcribe"
DEFAULT_MEMORY_MIB = 8192


@dataclass
class AsrBenchmarkParams:
    wav_path: str
    bundles: list[str] = field(default_factory=list)
    transcribe: bool = False
    reference_path: str | None = None
    diarize_system: bool = True
    diarize_mic: bool = False
    keep_bundle: bool = False
    template_bucket: str | None = None
    prices_file: str | None = None
    email_prefix: str = "lma-asr-benchmark"
    email_domain: str = "example.com"


@dataclass
class RunSpec:
    label: str
    engine: str
    bundle: str | None


def plan_runs(params: AsrBenchmarkParams) -> list[RunSpec]:
    runs = [RunSpec("transcribe", "transcribe", None)] if params.transcribe else []
    runs += [RunSpec(bundle, "microvm", bundle) for bundle in params.bundles]
    if not runs:
        raise ValueError("nothing to run: pass --bundles and/or --transcribe")
    return runs


def estimate(runs: list[RunSpec], wav_seconds: float, current: str | None) -> dict[str, Any]:
    switches = sum(1 for r in runs if r.bundle and r.bundle != current)
    minutes = (
        len(runs) * (wav_seconds / 60 + 3)
        + (switches + (1 if switches else 0)) * SWITCH_MINUTES_ESTIMATE
    )
    return {"runs": len(runs), "bundle_switches": switches, "minutes": round(minutes)}


def start_metadata(spec: RunSpec, params: AsrBenchmarkParams) -> dict[str, Any]:
    return {
        "asrEngine": spec.engine,
        "diarizeSystemChannel": params.diarize_system,
        "diarizeMicChannel": params.diarize_mic,
    }


def call_id_for(run_id: str, spec: RunSpec, attempt: int = 1) -> str:
    suffix = "" if attempt == 1 else f"-attempt{attempt}"
    return f"asr-bench-{run_id}-{spec.label}{suffix}"


def run(ctx: RunContext, stack: LMAStackInfo, params: AsrBenchmarkParams) -> list[dict[str, Any]]:
    ctx.assert_not_prod()
    catalog = bundles_mod.load_catalog()
    bundles_mod.validate_bundles(params.bundles, catalog)
    wav = _load_wav(Path(params.wav_path).expanduser().resolve())
    if wav.channels != 2:
        raise ValueError(
            f"{params.wav_path} has {wav.channels} channel(s); the transcriber expects stereo"
        )
    wav_seconds = len(wav.frames) / (wav.sample_rate * wav.channels * 2)
    runs = plan_runs(params)
    reference = quality.load_reference(params.reference_path) if params.reference_path else None

    session = boto3.Session(profile_name=ctx.profile, region_name=ctx.region)
    cfn = session.client("cloudformation")
    original = bundles_mod.current_bundle(cfn, ctx.stack_name) if params.bundles else None
    plan = estimate(runs, wav_seconds, original)
    console.print(
        f"[bold]ASR benchmark[/] {ctx.stack_name}: {len(runs)} run(s) of {wav_seconds / 60:.1f} min, "
        f"{plan['bundle_switches']} bundle switch(es), about {plan['minutes']} min. Current bundle: {original}"
    )
    for spec in runs:
        console.print(f"  - {spec.label} ({spec.engine})")
    ctx.write_json(
        "asr-benchmark-plan.json",
        {"runs": [r.__dict__ for r in runs], **plan, "original": original},
    )
    if ctx.dry_run:
        return []

    rates = measure.resolve_rates(
        session, ctx.region, Path(params.prices_file) if params.prices_file else None
    )
    logs = session.client("logs")
    ddb = session.client("dynamodb")
    transcriber_group = measure.transcriber_log_group(cfn, ctx.stack_name)
    engine_groups = measure.engine_log_groups(ctx.stack_name)

    pool = SyntheticUserPool(
        user_pool_id=stack.user_pool_id,
        user_pool_client_id=stack.user_pool_client_id,
        region=ctx.region,
        run_id=ctx.run_id,
        profile=ctx.profile,
    )
    pool.provision(
        count=1,
        email_prefix=params.email_prefix,
        email_domain=params.email_domain,
        admin_fraction=0,
    )
    user = pool.users[0]
    results: list[dict[str, Any]] = []
    switched = False
    try:
        for spec in runs:
            try:
                if spec.bundle:
                    switched = True
                    bundles_mod.switch_bundle(
                        session, ctx.stack_name, spec.bundle, ctx.run_id, params.template_bucket
                    )
                args = (
                    ctx,
                    stack,
                    params,
                    spec,
                    wav,
                    wav_seconds,
                    pool,
                    user,
                    rates,
                    logs,
                    ddb,
                    transcriber_group,
                    engine_groups,
                    catalog,
                )
                result = _one_run(*args)
                if result.get("warning") == FELL_BACK:
                    console.print(
                        f"{spec.label}: fell back to Amazon Transcribe; retrying in "
                        f"{FALLBACK_RETRY_WAIT_S}s"
                    )
                    time.sleep(FALLBACK_RETRY_WAIT_S)
                    retry = _one_run(*args, attempt=2)
                    retry["previous_attempt"] = result
                    result = retry
            except Exception as err:
                logger.exception("%s failed", spec.label)
                result = {
                    "label": spec.label,
                    "engine": spec.engine,
                    "error": f"{type(err).__name__}: {err}",
                }
            results.append(result)
            ctx.write_json("asr-benchmark.json", results)
    finally:
        if switched and original and not params.keep_bundle:
            console.print(f"Restoring {original}")
            bundles_mod.switch_bundle(
                session, ctx.stack_name, original, ctx.run_id, params.template_bucket
            )
        pool.delete_synthetic_users(ctx.run_id)

    compare(results, reference)
    ctx.write_json("asr-benchmark.json", results)
    table = render_table(results, rates, reference)
    (ctx.results_dir / "asr-benchmark.md").write_text(table + "\n")
    print(table)
    return results


def _one_run(
    ctx,
    stack,
    params,
    spec,
    wav,
    wav_seconds,
    pool,
    user,
    rates,
    logs,
    ddb,
    transcriber_group,
    engine_groups,
    catalog,
    attempt: int = 1,
) -> dict[str, Any]:
    pool.authenticate(user)
    call_id = call_id_for(ctx.run_id, spec, attempt)
    console.print(f"[bold]{spec.label}[/]: streaming {wav_seconds / 60:.1f} min as {call_id!r}")
    start_ms = int(time.time() * 1000)
    streamed = asyncio.run(
        _stream_one(
            idx=1,
            ws_url=stack.ws_endpoint,
            wav=wav,
            jwt_access=user.access_token,
            jwt_id=user.id_token,
            duration_s=wav_seconds,
            call_prefix="",
            agent_label="Microphone",
            call_id=call_id,
            extra_metadata=start_metadata(spec, params),
            play_once=True,
        )
    )
    result: dict[str, Any] = {
        "label": spec.label,
        "engine": spec.engine,
        "callId": call_id,
        "stream": streamed,
    }
    if streamed.get("status") != "ok":
        result["error"] = streamed.get("error")
        return result

    transcriber = measure.wait_for_release(logs, transcriber_group, call_id, start_ms, spec.engine)
    if spec.engine == "microvm" and transcriber.microvm_id and not transcriber.fell_back:
        engine = measure.parse_engine(
            measure.engine_events(logs, engine_groups, transcriber.microvm_id)
        )
        memory = int(
            catalog.get(spec.bundle or "", {}).get("baselineMemoryMiB", DEFAULT_MEMORY_MIB)
        )
        measurement = measure.price_microvm(transcriber, engine, memory, rates, wav_seconds)
        if engine.bundle and engine.bundle != spec.bundle:
            result["warning"] = f"engine reported bundle {engine.bundle}, expected {spec.bundle}"
    else:
        measurement = measure.price_transcribe(transcriber, rates, wav_seconds)
        if spec.engine == "microvm":
            result["warning"] = FELL_BACK
    result["measurement"] = measurement.to_dict()

    segments, shrank = quality.wait_for_segments(ddb, stack.event_sourcing_table, call_id)
    if shrank:
        result["warning"] = (
            "the transcript shrank while it was read; is transcript deletion after summary on?"
        )
    (ctx.results_dir / f"transcript-{spec.label}.txt").write_text(quality.render(segments) + "\n")
    result["segments"] = [s.__dict__ for s in segments]
    return result


def compare(results: list[dict[str, Any]], reference: quality.Reference | None) -> None:
    transcribe = next(
        (r for r in results if r["engine"] == "transcribe" and r.get("segments")), None
    )
    baseline = (
        quality.Reference([quality.Segment(**s) for s in transcribe["segments"]], by_channel=True)
        if transcribe
        else None
    )
    for result in results:
        segments = [quality.Segment(**s) for s in result.get("segments", [])]
        result["quality"] = quality.summarise(segments, reference)
        if baseline is not None and result is not transcribe:
            result["agreement_with_transcribe"] = quality.score(segments, baseline)


def _wer_cell(accuracy: dict[str, Any], channel: str) -> str:
    entry = accuracy.get(channel)
    if not entry:
        return ""
    return f"{entry['wer']:.1%} ({entry['swapped']:.1%} / {entry['missing']:.1%} / {entry['extra']:.1%})"


def _speaker_cell(q: dict[str, Any]) -> str:
    accuracy = q.get("accuracy") or {}
    cells = []
    for channel, labels in (q.get("speakers") or {}).items():
        expected = (accuracy.get(channel) or {}).get("reference_speakers")
        cells.append(f"{channel} {len(labels)}" + (f" of {expected}" if expected else ""))
    return ", ".join(cells)


def render_table(
    results: list[dict[str, Any]], rates: measure.Rates, reference: quality.Reference | None = None
) -> str:
    wer_channels = (
        []
        if reference is None
        else [("REMOTE", "WER meeting (swapped / missing / extra)"), ("LOCAL", "WER microphone")]
        if reference.by_channel
        else [("all", "WER both channels (swapped / missing / extra)")]
    )
    columns = [
        "Config",
        "Audio",
        "MicroVM",
        "Avg cores",
        "Peak cores",
        "RSS MB",
        "Cost",
        "$/audio h",
        "Cost, used cores",
        "$/audio h, used cores",
        "Transcribe $",
        "Rows",
        "Speakers",
    ]
    columns += [label for _, label in wer_channels]
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for r in results:
        m = r.get("measurement") or {}
        q = r.get("quality") or {}
        cells = [
            r["label"],
            _minutes(m.get("audio_seconds")),
            _minutes(m.get("microvm_seconds")),
            _num(m.get("avg_cores")),
            _num(m.get("peak_cores")),
            str(m.get("peak_rss_mb") or ""),
            _money(m.get("cost")),
            _money(m.get("cost_per_audio_hour")),
            _money(m.get("cost_at_used_cores")),
            _money(m.get("cost_at_used_cores_per_audio_hour")),
            _money(m.get("transcribe_equivalent")),
            str(q.get("rows", "")),
            _speaker_cell(q),
        ]
        accuracy = q.get("accuracy") or {}
        cells += [_wer_cell(accuracy, channel) for channel, _ in wer_channels]
        lines.append("| " + " | ".join(cells) + " |")
        if r.get("error") or r.get("warning"):
            lines.append(f"| ↳ {r.get('error') or r.get('warning')} |" + " |" * (len(columns) - 1))
    lines.append("")
    lines.append(
        f"Prices ({rates.source}): vCPU ${rates.vcpu_second:.10f}/s, memory ${rates.gb_second:.10f}/GB-s, "
        f"Transcribe ${rates.transcribe_second:.7f}/s. Cost bills vCPUs at the larger of the baseline "
        "(memory / 2 GiB) and the cores in use, as MicroVMs are billed; Cost, used cores bills only the "
        "cores the engine measured, with memory unchanged."
    )
    if reference is not None:
        lines.append(
            "WER is scored per channel against the reference after normalisation (case, punctuation, "
            "fillers, okay/ok, numbers up to 999, -isation/-ization)."
        )
    return "\n".join(lines)


def _minutes(seconds: float | None) -> str:
    return "" if seconds is None else f"{seconds / 60:.1f} min"


def _num(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def _money(value: float | None) -> str:
    return "" if value is None else f"${value:,.3f}"
