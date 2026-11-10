# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""What one benchmark run used, read from the transcriber's and the engine's logs, and its price."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRANSCRIBE_MIN_BILLED_SECONDS = 15.0
MIB_PER_VCPU = 2048
PRICING_RELATIVE = (
    Path("lma-ai-stack")
    / "source"
    / "lambda_functions"
    / "configuration_seeder"
    / "defaults"
    / "CallPricing.json"
)

ACQUIRED_RE = re.compile(r"Acquired ASR MicroVM (?P<id>microvm-[0-9a-f-]+)")
RELEASED_RE = re.compile(r"released ASR MicroVM (?P<id>microvm-[0-9a-f-]+)")
FALLBACK_RE = re.compile(r"falling back to Amazon Transcribe")
REQUEST_START_RE = re.compile(
    r"Initializing Transcribe streaming|Retry Transcribe streaming attempt"
)
REQUEST_END_RE = re.compile(
    r"Attempting to restart session|closed unexpectedly; reconnecting|"
    r"Call ended; not \(re\)starting|Max consecutive retries"
)
LOAD_RE = re.compile(
    r"engine load: (?P<used>[\d.]+) of (?P<cores>\d+) cores .*?over the last (?P<secs>[\d.]+)s"
    r"(?:.*?rss=(?P<rss>\d+)MB)?"
)
SESSION_START_RE = re.compile(
    r"session (?P<sid>\S+) start: .*?engine=(?P<engine>\S+)(?: bundle=(?P<bundle>\S+))?"
)
SESSION_END_RE = re.compile(r"session (?P<sid>\S+) end \(.*?\(~(?P<audio>[\d.]+)s audio\)")


@dataclass
class Rates:
    vcpu_second: float
    gb_second: float
    transcribe_second: float
    source: str


@dataclass
class TranscriberRun:
    microvm_id: str | None = None
    acquired: float | None = None
    released: float | None = None
    fell_back: bool = False
    requests: list[tuple[float, float]] = field(default_factory=list)
    open_request: float | None = None
    last_seen: float = 0.0

    def transcribe_seconds(self) -> tuple[float, float]:
        spans = list(self.requests)
        if self.open_request is not None:
            spans.append((self.open_request, self.last_seen))
        streamed = sum(max(0.0, b - a) for a, b in spans)
        billed = sum(max(TRANSCRIBE_MIN_BILLED_SECONDS, b - a) for a, b in spans)
        return streamed, billed


@dataclass
class EngineRun:
    first: float = 0.0
    last: float = 0.0
    samples: list[tuple[float, float]] = field(default_factory=list)
    peak_rss_mb: int = 0
    cores: int = 0
    bundle: str = ""
    engine: str = ""
    audio_seconds: dict[str, float] = field(default_factory=dict)

    @property
    def span(self) -> float:
        return max(0.0, self.last - self.first)

    @property
    def avg_cores(self) -> float | None:
        sampled = sum(secs for _, secs in self.samples)
        return sum(u * s for u, s in self.samples) / sampled if sampled else None

    @property
    def core_seconds(self) -> float:
        return sum(u * s for u, s in self.samples)

    @property
    def peak_cores(self) -> float | None:
        return max((u for u, _ in self.samples), default=None)

    def billed_vcpu_seconds(self, lifetime: float, baseline_vcpus: float) -> float:
        sampled = sum(max(baseline_vcpus, u) * s for u, s in self.samples)
        unsampled = max(0.0, lifetime - sum(s for _, s in self.samples))
        return sampled + baseline_vcpus * unsampled


def parse_transcriber(events: list[tuple[float, str]]) -> TranscriberRun:
    run = TranscriberRun()
    for ts, message in sorted(events):
        run.last_seen = max(run.last_seen, ts)
        if m := ACQUIRED_RE.search(message):
            run.microvm_id, run.acquired = m.group("id"), ts
        elif m := RELEASED_RE.search(message):
            run.microvm_id, run.released = run.microvm_id or m.group("id"), ts
        elif FALLBACK_RE.search(message):
            run.fell_back = True
        elif REQUEST_START_RE.search(message):
            if run.open_request is not None:
                run.requests.append((run.open_request, ts))
            run.open_request = ts
        elif REQUEST_END_RE.search(message) and run.open_request is not None:
            run.requests.append((run.open_request, ts))
            run.open_request = None
    return run


def parse_engine(events: list[tuple[float, str]]) -> EngineRun:
    run = EngineRun()
    if not events:
        return run
    run.first = min(ts for ts, _ in events)
    run.last = max(ts for ts, _ in events)
    for _, message in events:
        if m := LOAD_RE.search(message):
            run.samples.append((float(m.group("used")), float(m.group("secs"))))
            run.cores = int(m.group("cores"))
            if m.group("rss"):
                run.peak_rss_mb = max(run.peak_rss_mb, int(m.group("rss")))
        elif m := SESSION_START_RE.search(message):
            run.engine = m.group("engine")
            run.bundle = m.group("bundle") or run.bundle
        elif m := SESSION_END_RE.search(message):
            run.audio_seconds[m.group("sid")] = float(m.group("audio"))
    return run


@dataclass
class Measurement:
    engine: str
    bundle: str
    audio_seconds: float
    cost: float
    transcribe_equivalent: float
    microvm_seconds: float | None = None
    avg_cores: float | None = None
    peak_cores: float | None = None
    peak_rss_mb: int | None = None
    billed_vcpu_seconds: float | None = None
    used_vcpu_seconds: float | None = None
    cost_at_used_cores: float | None = None
    fell_back: bool = False

    def _per_audio_hour(self, cost: float | None) -> float | None:
        if cost is None or not self.audio_seconds:
            return None
        return cost / self.audio_seconds * 3600

    @property
    def cost_per_audio_hour(self) -> float:
        return self._per_audio_hour(self.cost) or 0.0

    @property
    def cost_at_used_cores_per_audio_hour(self) -> float | None:
        return self._per_audio_hour(self.cost_at_used_cores)

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["cost_per_audio_hour"] = self.cost_per_audio_hour
        out["cost_at_used_cores_per_audio_hour"] = self.cost_at_used_cores_per_audio_hour
        return out


def price_microvm(
    transcriber: TranscriberRun,
    engine: EngineRun,
    memory_mib: int,
    rates: Rates,
    wav_seconds: float,
) -> Measurement:
    lifetime = engine.span
    if transcriber.acquired is not None and transcriber.released is not None:
        lifetime = max(lifetime, transcriber.released - transcriber.acquired)
    baseline = memory_mib / MIB_PER_VCPU
    vcpu_seconds = engine.billed_vcpu_seconds(lifetime, baseline)
    gb = memory_mib / 1024
    memory_cost = gb * lifetime * rates.gb_second
    cost = memory_cost + vcpu_seconds * rates.vcpu_second
    used = engine.core_seconds
    audio = max(engine.audio_seconds.values(), default=0.0) or wav_seconds
    return Measurement(
        engine="microvm",
        bundle=engine.bundle,
        audio_seconds=audio,
        cost=cost,
        transcribe_equivalent=audio * rates.transcribe_second,
        microvm_seconds=lifetime,
        avg_cores=engine.avg_cores,
        peak_cores=engine.peak_cores,
        peak_rss_mb=engine.peak_rss_mb or None,
        billed_vcpu_seconds=vcpu_seconds,
        used_vcpu_seconds=used,
        cost_at_used_cores=memory_cost + used * rates.vcpu_second,
        fell_back=transcriber.fell_back,
    )


def price_transcribe(transcriber: TranscriberRun, rates: Rates, wav_seconds: float) -> Measurement:
    requests = len(transcriber.requests) + (transcriber.open_request is not None)
    streamed = wav_seconds
    billed = max(TRANSCRIBE_MIN_BILLED_SECONDS, wav_seconds) + TRANSCRIBE_MIN_BILLED_SECONDS * max(
        0, requests - 1
    )
    cost = billed * rates.transcribe_second
    return Measurement(
        engine="transcribe",
        bundle="",
        audio_seconds=streamed,
        cost=cost,
        transcribe_equivalent=cost,
        fell_back=transcriber.fell_back,
    )


def find_pricing_file(start: Path | None = None) -> Path | None:
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / PRICING_RELATIVE
        if candidate.exists():
            return candidate
    return None


def rates_from_pricing_file(path: Path, arch: str = "arm64") -> Rates:
    services = json.loads(path.read_text())["regions"]["default"]
    microvm = services[f"lambda-microvm/{arch}"]
    return Rates(
        vcpu_second=float(microvm["vcpuSeconds"]),
        gb_second=float(microvm["gbSeconds"]),
        transcribe_second=float(services["transcribe/streaming"]["seconds"]),
        source=str(path.name),
    )


def _first_tier(products: list[dict]) -> float:
    best: tuple[float, float] | None = None
    for product in products:
        for term in product["terms"].get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                begin = float(dim.get("beginRange", "0"))
                price = float(dim["pricePerUnit"]["USD"])
                if best is None or begin < best[0]:
                    best = (begin, price)
    if best is None:
        raise LookupError("no on-demand price")
    return best[1]


def streaming_audio_products(products: list[dict]) -> list[dict]:
    matched = [
        p
        for p in products
        if str(p.get("product", {}).get("attributes", {}).get("usagetype", "")).split("-")[-1]
        == "StreamingAudio"
    ]
    if len(matched) != 1:
        raise LookupError(f"expected one Transcribe streaming price, found {len(matched)}")
    return matched


def fetch_rates(session, region: str, arch: str = "arm64") -> Rates:
    pricing = session.client("pricing", region_name="us-east-1")

    def products(service: str, filters: dict[str, str]) -> list[dict]:
        query = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]
        out, token = [], None
        while True:
            kwargs: dict[str, Any] = {"ServiceCode": service, "Filters": query}
            if token:
                kwargs["NextToken"] = token
            page = pricing.get_products(**kwargs)
            out += [json.loads(p) for p in page["PriceList"]]
            token = page.get("NextToken")
            if not token:
                return out

    suffix = "-ARM" if arch == "arm64" else ""
    return Rates(
        vcpu_second=_first_tier(
            products(
                "AWSLambda",
                {"regionCode": region, "group": f"AWS-Lambda-MicroVM-vCPU-Second{suffix}"},
            )
        ),
        gb_second=_first_tier(
            products(
                "AWSLambda",
                {"regionCode": region, "group": f"AWS-Lambda-MicroVM-Memory-GB-Second{suffix}"},
            )
        ),
        transcribe_second=_first_tier(
            streaming_audio_products(
                products("transcribe", {"regionCode": region, "operation": "StreamingAudio"})
            )
        ),
        source="AWS Price List API",
    )


def resolve_rates(session, region: str, prices_file: Path | None = None) -> Rates:
    if prices_file:
        return rates_from_pricing_file(prices_file)
    try:
        return fetch_rates(session, region)
    except Exception:
        fallback = find_pricing_file()
        if not fallback:
            raise
        return rates_from_pricing_file(fallback)


def transcriber_log_group(cfn, stack_name: str) -> str:
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack_name):
        for resource in page["StackResourceSummaries"]:
            if (
                resource["ResourceType"] == "AWS::CloudFormation::Stack"
                and "WEBSOCKETTRANSCRIBER" in resource["LogicalResourceId"].upper()
            ):
                nested = resource["PhysicalResourceId"]
                for inner_page in cfn.get_paginator("list_stack_resources").paginate(
                    StackName=nested
                ):
                    for inner in inner_page["StackResourceSummaries"]:
                        if (
                            inner["ResourceType"] == "AWS::Logs::LogGroup"
                            and "WebsocketContainer" in inner["LogicalResourceId"]
                        ):
                            return inner["PhysicalResourceId"]
    raise LookupError(f"no transcriber log group found under {stack_name}")


def engine_log_groups(stack_name: str) -> list[str]:
    return [f"/aws/lambda-microvms/{stack_name}-asr-logs", f"/aws/lambda-microvms/{stack_name}-asr"]


def transcriber_events(
    logs, group: str, call_id: str, start_ms: int, end_ms: int
) -> list[tuple[float, str]]:
    events: list[tuple[float, str]] = []
    for page in logs.get_paginator("filter_log_events").paginate(
        logGroupName=group, startTime=start_ms, endTime=end_ms, filterPattern=json.dumps(call_id)
    ):
        events += [(e["timestamp"] / 1000, e["message"]) for e in page["events"]]
    return events


def engine_events(logs, groups: list[str], microvm_id: str) -> list[tuple[float, str]]:
    for group in groups:
        try:
            events = _engine_events_in(logs, group, microvm_id)
        except logs.exceptions.ResourceNotFoundException:
            continue
        if events:
            return events
    return []


def _engine_events_in(logs, group: str, microvm_id: str) -> list[tuple[float, str]]:
    stream = None
    for page in logs.get_paginator("describe_log_streams").paginate(
        logGroupName=group, orderBy="LastEventTime", descending=True
    ):
        stream = next(
            (s["logStreamName"] for s in page["logStreams"] if microvm_id in s["logStreamName"]),
            None,
        )
        if stream:
            break
    if not stream:
        return []
    events: list[tuple[float, str]] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {
            "logGroupName": group,
            "logStreamName": stream,
            "startFromHead": True,
        }
        if token:
            kwargs["nextToken"] = token
        page = logs.get_log_events(**kwargs)
        events += [(e["timestamp"] / 1000, e["message"]) for e in page["events"]]
        if not page["events"] or page.get("nextForwardToken") == token:
            return events
        token = page["nextForwardToken"]


def wait_for_release(
    logs,
    group: str,
    call_id: str,
    start_ms: int,
    engine: str,
    timeout_s: float = 600,
    poll_s: float = 15,
) -> TranscriberRun:
    deadline = time.monotonic() + timeout_s
    while True:
        run = parse_transcriber(
            transcriber_events(logs, group, call_id, start_ms, int(time.time() * 1000) + 60_000)
        )
        if engine == "microvm" and not run.fell_back:
            done = run.released is not None
        else:
            done = bool(run.requests) and run.open_request is None
        if done or time.monotonic() > deadline:
            return run
        time.sleep(poll_s)
