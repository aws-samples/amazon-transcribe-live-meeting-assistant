# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""ASR benchmark: bundle switching, measurement, pricing, quality and the CLI."""

from __future__ import annotations

import asyncio
import json

import pytest
from botocore.exceptions import ClientError
from click.testing import CliRunner

from lma_load.asr_benchmark import bundles, measure, quality
from lma_load.cli import load_command
from lma_load.drivers import ws_streaming_driver as ws
from lma_load.scenarios import asr_benchmark as bench

VM = "microvm-6f9b00bb-a70e-3cfc-8591-9a028360b9d2"
T0 = 1_793_200_000.0
TEMPLATE = (
    "Mappings:\n  AsrDefaults:\n    Engine:\n      ModelBundle: parakeet-tdt-v3-sortformer\n"
    '      MaxMeetingSeconds: "14400"\nResources:\n  ASRMICROVMSTACK:\n    Properties:\n'
    "      TemplateURL: https://s3.us-east-1.amazonaws.com/lma-1234-us-east-1/lma/0.3/asr.yaml\n"
)
RATES = measure.Rates(
    vcpu_second=0.00002, gb_second=0.000004, transcribe_second=0.0002, source="test"
)


def _line(call: str, text: str, kind: str = "ASR") -> str:
    return f"INFO [14:05:20.688]: [{kind}]: [{call}] - {text}"


def test_the_bundle_line_is_the_only_change_to_the_template() -> None:
    rewritten = bundles.rewrite_bundle(TEMPLATE, "parakeet-tdt-v3-titanet-small")
    assert rewritten == TEMPLATE.replace(
        "parakeet-tdt-v3-sortformer", "parakeet-tdt-v3-titanet-small"
    )


@pytest.mark.parametrize("template", ["Mappings: {}\n", TEMPLATE + "      ModelBundle: other\n"])
def test_a_template_without_exactly_one_bundle_line_is_refused(template: str) -> None:
    with pytest.raises(ValueError, match="expected one"):
        bundles.rewrite_bundle(template, "x")


@pytest.mark.parametrize(
    "url",
    [
        "https://s3.us-east-1.amazonaws.com/lma-1234-us-east-1/lma/asr.yaml",
        "https://lma-1234-us-east-1.s3.us-east-1.amazonaws.com/lma/asr.yaml",
    ],
)
def test_the_template_bucket_is_read_from_a_nested_template_url(url: str) -> None:
    assert bundles.template_bucket(f"      TemplateURL: {url}\n") == "lma-1234-us-east-1"


def test_bundle_ids_are_checked_against_the_repository_catalog() -> None:
    catalog = bundles.load_catalog()
    assert "parakeet-tdt-v3-titanet-small" in catalog
    bundles.validate_bundles(["parakeet-tdt-v3-titanet-small"], catalog)
    with pytest.raises(ValueError, match="unknown bundle"):
        bundles.validate_bundles(["no-such-bundle"], catalog)


class _FakeWaiter:
    def __init__(self, cfn) -> None:
        self.cfn = cfn

    def wait(self, **kwargs) -> None:
        self.cfn.bundle = self.cfn.pending


class _FakeCfn:
    def __init__(self, bundle: str, fail_with: str | None = None) -> None:
        self.bundle = bundle
        self.pending = bundle
        self.updates: list[dict] = []
        self.fail_with = fail_with

    def describe_stack_resource(self, **kwargs):
        return {"StackResourceDetail": {"PhysicalResourceId": "nested-asr"}}

    def describe_stacks(self, StackName: str):
        if StackName == "nested-asr":
            return {
                "Stacks": [
                    {"Outputs": [{"OutputKey": "AsrModelBundleId", "OutputValue": self.bundle}]}
                ]
            }
        return {
            "Stacks": [
                {
                    "Parameters": [
                        {"ParameterKey": "AdminEmail"},
                        {"ParameterKey": "EnableMicrovmAsr"},
                    ],
                    "Capabilities": ["CAPABILITY_IAM"],
                }
            ]
        }

    def get_template(self, **kwargs):
        return {"TemplateBody": TEMPLATE}

    def update_stack(self, **kwargs):
        if self.fail_with:
            raise ClientError(
                {"Error": {"Code": "ValidationError", "Message": self.fail_with}}, "UpdateStack"
            )
        self.updates.append(kwargs)
        self.pending = kwargs["TemplateURL"].rsplit("lma-main-", 1)[1].removesuffix(".yaml")

    def get_waiter(self, name: str):
        return _FakeWaiter(self)


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:
        self.objects[(Bucket, Key)] = Body


class _FakeSession:
    region_name = "us-east-1"

    def __init__(self, cfn: _FakeCfn) -> None:
        self.cfn, self.s3 = cfn, _FakeS3()

    def client(self, name: str, **kwargs):
        return {"cloudformation": self.cfn, "s3": self.s3}[name]


def test_switching_updates_the_stack_with_every_parameter_kept() -> None:
    session = _FakeSession(_FakeCfn("parakeet-tdt-v3-sortformer"))
    assert bundles.switch_bundle(session, "LMA-dev", "parakeet-tdt-v3-titanet-small", "asrb-1")
    (update,) = session.cfn.updates
    assert update["Parameters"] == [
        {"ParameterKey": "AdminEmail", "UsePreviousValue": True},
        {"ParameterKey": "EnableMicrovmAsr", "UsePreviousValue": True},
    ]
    assert update["Capabilities"] == ["CAPABILITY_IAM"]
    assert update["TemplateURL"] == (
        "https://s3.us-east-1.amazonaws.com/lma-1234-us-east-1/"
        "asr-benchmark/asrb-1/lma-main-parakeet-tdt-v3-titanet-small.yaml"
    )
    body = session.s3.objects[
        ("lma-1234-us-east-1", "asr-benchmark/asrb-1/lma-main-parakeet-tdt-v3-titanet-small.yaml")
    ]
    assert b"ModelBundle: parakeet-tdt-v3-titanet-small" in body


def test_a_stack_already_on_the_bundle_is_not_updated() -> None:
    session = _FakeSession(_FakeCfn("parakeet-tdt-v3-titanet-small"))
    assert not bundles.switch_bundle(session, "LMA-dev", "parakeet-tdt-v3-titanet-small", "asrb-1")
    assert session.cfn.updates == []


def test_a_stack_update_with_nothing_to_change_counts_as_no_switch() -> None:
    session = _FakeSession(_FakeCfn("a", fail_with="No updates are to be performed."))
    assert not bundles.switch_bundle(session, "LMA-dev", "b", "asrb-1")


def test_a_microvm_run_is_read_from_the_transcriber_log() -> None:
    run = measure.parse_transcriber(
        [
            (T0, _line("bench", f"Acquired ASR MicroVM {VM} in 13.3s")),
            (T0 + 1638, _line("bench", f"released ASR MicroVM {VM}")),
        ]
    )
    assert (run.microvm_id, run.released - run.acquired, run.fell_back) == (VM, 1638, False)


def test_transcribe_requests_are_billed_per_request_with_the_minimum() -> None:
    run = measure.parse_transcriber(
        [
            (
                T0,
                _line(
                    "bench",
                    "Initializing Transcribe streaming - no existing sessionId",
                    "TRANSCRIBING",
                ),
            ),
            (
                T0 + 200,
                _line("bench", "Attempting to restart session. Retry count: 0", "TRANSCRIBING"),
            ),
            (
                T0 + 201,
                _line(
                    "bench",
                    "Retry Transcribe streaming attempt 0 - use existing sessionId",
                    "TRANSCRIBING",
                ),
            ),
            (
                T0 + 205,
                _line(
                    "bench", "Call ended; not (re)starting transcription session.", "TRANSCRIBING"
                ),
            ),
        ]
    )
    assert run.transcribe_seconds() == (204, 215)


def _engine_log() -> list[tuple[float, str]]:
    return [
        (
            T0,
            "session a start: sample_rate=16000 interim_results=True engine=two_pass bundle=b-1 word_timestamps=False",
        ),
        (
            T0 + 60,
            "engine load: 10.00 of 16 cores (62%) over the last 60s, 2 session(s), rss=2306MB",
        ),
        (
            T0 + 120,
            "engine load: 2.00 of 16 cores (12%) over the last 60s, 2 session(s), rss=2401MB",
        ),
        (
            T0 + 150,
            "session a end (graceful): frames_in=1 bytes_in=1 (~149.0s audio) recognizer_events=1",
        ),
        (T0 + 180, "[hooks] stop"),
    ]


def test_the_engine_log_gives_cpu_memory_bundle_and_audio() -> None:
    engine = measure.parse_engine(_engine_log())
    assert (engine.bundle, engine.engine, engine.cores, engine.peak_rss_mb) == (
        "b-1",
        "two_pass",
        16,
        2401,
    )
    assert engine.avg_cores == pytest.approx(6.0)
    assert engine.peak_cores == 10.0
    assert engine.span == 180


def test_vcpus_below_the_baseline_are_billed_at_the_baseline() -> None:
    engine = measure.parse_engine(_engine_log())
    assert engine.billed_vcpu_seconds(180, 4.0) == pytest.approx(10 * 60 + 4 * 60 + 4 * 60)


def test_a_microvm_run_is_priced_from_memory_lifetime_and_billed_vcpus() -> None:
    transcriber = measure.parse_transcriber(
        [
            (T0, _line("b", f"Acquired ASR MicroVM {VM} in 5s")),
            (T0 + 200, _line("b", f"released ASR MicroVM {VM}")),
        ]
    )
    m = measure.price_microvm(transcriber, measure.parse_engine(_engine_log()), 8192, RATES, 150)
    assert m.microvm_seconds == 200
    assert m.billed_vcpu_seconds == pytest.approx(600 + 240 + 4 * 80)
    assert m.cost == pytest.approx(8 * 200 * 0.000004 + (600 + 240 + 320) * 0.00002)
    assert m.audio_seconds == 149.0
    assert m.transcribe_equivalent == pytest.approx(149 * 0.0002)


def test_the_repository_price_table_supplies_fallback_rates() -> None:
    path = measure.find_pricing_file()
    assert path is not None
    rates = measure.rates_from_pricing_file(path)
    assert rates.vcpu_second > 0 and rates.gb_second > 0 and rates.transcribe_second > 0


def test_a_transcribe_run_bills_the_audio_plus_a_minimum_per_reconnect() -> None:
    transcriber = measure.TranscriberRun(requests=[(T0, T0 + 100), (T0 + 101, T0 + 160)])
    m = measure.price_transcribe(transcriber, RATES, 150)
    assert m.audio_seconds == 150
    assert m.cost == pytest.approx((150 + 15) * 0.0002)
    short = measure.price_transcribe(measure.TranscriberRun(requests=[(T0, T0 + 5)]), RATES, 5)
    assert short.cost == pytest.approx(15 * 0.0002)


def test_the_lowest_price_tier_is_used() -> None:
    dims = {
        "a": {"beginRange": "15000000", "pricePerUnit": {"USD": "0.0003"}},
        "b": {"beginRange": "0", "pricePerUnit": {"USD": "0.0005"}},
    }
    assert (
        measure._first_tier([{"terms": {"OnDemand": {"t": {"priceDimensions": dims}}}}]) == 0.0005
    )


@pytest.mark.parametrize(
    ("reference", "hypothesis", "expected"),
    [
        ("the cat sat", "the cat sat", 0.0),
        ("the cat sat", "the bat sat", 1 / 3),
        ("the cat sat", "the cat", 1 / 3),
        ("the cat", "the big cat", 1 / 2),
        ("", "", 0.0),
    ],
)
def test_word_error_rate(reference: str, hypothesis: str, expected: float) -> None:
    assert quality.word_error_rate(
        quality.normalise(reference), quality.normalise(hypothesis)
    ) == pytest.approx(expected)


def test_case_and_punctuation_do_not_count_as_errors() -> None:
    assert quality.normalise("Hello, World! It's") == ["hello", "world", "it's"]


class _FakeDdb:
    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        page = {"Items": self.pages[index]}
        if index + 1 < len(self.pages):
            page["LastEvaluatedKey"] = {"PK": {"S": str(index)}}
        return page


def _item(start: float, channel: str, speaker: str, text: str) -> dict:
    return {
        "Channel": {"S": channel},
        "Speaker": {"S": speaker},
        "StartTime": {"N": str(start)},
        "EndTime": {"N": str(start + 1)},
        "Transcript": {"S": text},
    }


def test_segments_are_read_across_pages_in_time_order() -> None:
    ddb = _FakeDdb([[_item(5, "CALLER", "A", "later")], [_item(1, "AGENT", "Me", "first")]])
    segments = quality.read_segments(ddb, "table", "call")
    assert [s.text for s in segments] == ["first", "later"]
    assert ddb.calls[1]["ExclusiveStartKey"] == {"PK": {"S": "0"}}
    assert ddb.calls[0]["ExpressionAttributeValues"][":pk"] == {"S": "trs#call"}


def test_a_summary_counts_rows_words_and_speakers_per_channel() -> None:
    segments = [
        quality.Segment("CALLER", "Other (spk_0)", 0, 1, "hello there"),
        quality.Segment("CALLER", "Other (spk_1)", 1, 2, "hi"),
        quality.Segment("AGENT", "Me", 2, 3, "good morning"),
    ]
    summary = quality.summarise(segments, "hello there hi good morning")
    assert summary["rows"] == 3 and summary["words"] == 5 and summary["wer"] == 0.0
    assert summary["speakers"] == {"AGENT": ["Me"], "CALLER": ["Other (spk_0)", "Other (spk_1)"]}


def test_runs_put_transcribe_first_and_name_each_meeting() -> None:
    params = bench.AsrBenchmarkParams(wav_path="x.wav", bundles=["a", "b"], transcribe=True)
    runs = bench.plan_runs(params)
    assert [(r.label, r.engine) for r in runs] == [
        ("transcribe", "transcribe"),
        ("a", "microvm"),
        ("b", "microvm"),
    ]
    assert bench.call_id_for("asrb-1", runs[1]) == "asr-bench-asrb-1-a"
    assert bench.start_metadata(runs[1], params) == {
        "asrEngine": "microvm",
        "diarizeSystemChannel": True,
        "diarizeMicChannel": False,
    }
    assert bench.estimate(runs, 600, current="a")["bundle_switches"] == 1


def test_an_empty_plan_is_refused() -> None:
    with pytest.raises(ValueError, match="nothing to run"):
        bench.plan_runs(bench.AsrBenchmarkParams(wav_path="x.wav"))


def test_without_a_reference_each_run_is_compared_with_transcribe() -> None:
    results = [
        {
            "engine": "transcribe",
            "segments": [quality.Segment("CALLER", "A", 0, 1, "one two three").__dict__],
        },
        {
            "engine": "microvm",
            "segments": [quality.Segment("CALLER", "A", 0, 1, "one too three").__dict__],
        },
    ]
    bench.compare(results, None)
    assert results[1]["quality"]["wer"] == pytest.approx(1 / 3)
    table = bench.render_table(
        [
            {
                "label": "b",
                "engine": "microvm",
                "measurement": {"cost": 0.5},
                "quality": results[1]["quality"],
            }
        ],
        RATES,
        have_reference=False,
    )
    assert "vs Transcribe" in table and "33.3%" in table and "$0.500" in table


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, data) -> None:
        self.sent.append(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def test_play_once_sends_the_file_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "CHUNK_SIZE_MS", 1)
    sock, progress = _FakeSocket(), {"sent": 0}
    asyncio.run(ws._send_paced(sock, b"abcdefghij", 4, 999, True, progress))
    assert sock.sent == [b"abcd", b"efgh", b"ij"]
    assert progress["sent"] == 10


def test_the_start_frame_carries_the_engine_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    import websockets

    sock = _FakeSocket()
    monkeypatch.setattr(ws, "CHUNK_SIZE_MS", 1)
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: sock)
    wav = ws._LoadedWav(sample_rate=16000, channels=2, frames=b"\0" * 8, chunk_bytes=4)
    result = asyncio.run(
        ws._stream_one(
            1,
            "wss://x/api/v1/ws",
            wav,
            "a",
            "i",
            0,
            "",
            "me",
            call_id="bench-1",
            extra_metadata={"asrEngine": "microvm"},
            play_once=True,
        )
    )
    start, end = json.loads(sock.sent[0]), json.loads(sock.sent[-1])
    assert (start["callEvent"], start["callId"], start["asrEngine"]) == (
        "START",
        "bench-1",
        "microvm",
    )
    assert end["callEvent"] == "END"
    assert sock.sent[1:-1] == [b"\0" * 4, b"\0" * 4]
    assert result["status"] == "ok" and result["bytes_sent"] == 8


def test_the_cli_lists_the_benchmark_and_needs_something_to_run(tmp_path) -> None:
    runner = CliRunner()
    assert "asr-benchmark" in runner.invoke(load_command, ["--help"]).output
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"")
    result = runner.invoke(
        load_command, ["asr-benchmark", "-s", "dev", "--region", "us-east-1", "--wav", str(wav)]
    )
    assert result.exit_code != 0
    assert "--bundles and/or --transcribe" in result.output


class _NotFound(Exception):
    pass


class _FakeLogs:
    class exceptions:
        ResourceNotFoundException = _NotFound

    def __init__(self, streams: dict[str, list[str]], events: list[tuple[float, str]]) -> None:
        self.streams, self.events = streams, events

    def get_paginator(self, name: str):
        logs = self

        class _Paginator:
            def paginate(self, logGroupName: str, **kwargs):
                if logGroupName not in logs.streams:
                    raise _NotFound(logGroupName)
                yield {"logStreams": [{"logStreamName": n} for n in logs.streams[logGroupName]]}

        return _Paginator()

    def get_log_events(self, **kwargs):
        if kwargs.get("nextToken"):
            return {"events": [], "nextForwardToken": kwargs["nextToken"]}
        return {
            "events": [{"timestamp": ts * 1000, "message": m} for ts, m in self.events],
            "nextForwardToken": "t1",
        }


def test_engine_logs_are_found_in_the_older_group_name_too() -> None:
    logs = _FakeLogs({"/aws/lambda-microvms/dev-asr": [f"2026/10/28[21.0]{VM}"]}, _engine_log())
    events = measure.engine_events(logs, measure.engine_log_groups("dev"), VM)
    assert len(events) == len(_engine_log())
    assert measure.engine_events(logs, measure.engine_log_groups("dev"), "microvm-other") == []


def test_a_failed_run_is_recorded_and_the_next_one_still_runs(monkeypatch, tmp_path) -> None:
    import wave as wave_mod

    path = tmp_path / "a.wav"
    with wave_mod.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\0" * 6400)
    switched: list[str] = []

    def fake_switch(session, stack, bundle, run_id, bucket=None):
        switched.append(bundle)
        if bundle == "parakeet-tdt-v3-sortformer":
            raise RuntimeError("image build failed")
        return True

    class _Pool:
        def __init__(self, **kwargs) -> None:
            self.users = [type("U", (), {"email": "u", "access_token": "a", "id_token": "i"})()]

        def provision(self, **kwargs) -> None:
            pass

        def delete_synthetic_users(self, run_id) -> int:
            return 1

    class _Session:
        def __init__(self, **kwargs) -> None:
            pass

        def client(self, name, **kwargs):
            return None

    monkeypatch.setattr(bench.bundles_mod, "current_bundle", lambda cfn, stack: "orig")
    monkeypatch.setattr(bench.bundles_mod, "switch_bundle", fake_switch)
    monkeypatch.setattr(bench.boto3, "Session", _Session)
    monkeypatch.setattr(bench, "SyntheticUserPool", _Pool)
    monkeypatch.setattr(bench.measure, "resolve_rates", lambda *a: RATES)
    monkeypatch.setattr(bench.measure, "transcriber_log_group", lambda cfn, stack: "g")
    monkeypatch.setattr(
        bench,
        "_one_run",
        lambda ctx, stack, params, spec, *rest: {"label": spec.label, "engine": spec.engine},
    )
    ctx = bench.RunContext(stack_name="LMA-dev", region="us-east-1", results_dir=tmp_path)
    params = bench.AsrBenchmarkParams(
        wav_path=str(path), bundles=["parakeet-tdt-v3-sortformer", "parakeet-tdt-v3-titanet-small"]
    )
    results = bench.run(
        ctx, type("S", (), {"user_pool_id": "p", "user_pool_client_id": "c"})(), params
    )

    assert [r["label"] for r in results] == [
        "parakeet-tdt-v3-sortformer",
        "parakeet-tdt-v3-titanet-small",
    ]
    assert results[0]["error"] == "RuntimeError: image build failed"
    assert switched == ["parakeet-tdt-v3-sortformer", "parakeet-tdt-v3-titanet-small", "orig"]
