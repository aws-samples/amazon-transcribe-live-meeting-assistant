# ASR Model Benchmark — LMA (cost and quality of model configurations)

Use this skill when asked to **compare ASR models or bundles**, **benchmark the
MicroVM engine against Amazon Transcribe**, **measure what a model configuration
costs**, or **check transcript quality before changing the default bundle**. It is
a controlled lab test on a dev stack: the same recording goes through every
configuration. It is not the per-meeting cost estimate (`docs/meeting-cost-estimate.md`),
which prices real meetings.

The tool is the load simulator's `asr-benchmark` scenario
(`utilities/load-simulator/lma_load/scenarios/asr_benchmark.py`).

## How to run it

```bash
# Dry run first: prints the plan, the bundle switches and the expected time.
AWS_PROFILE=default make asr-benchmark STACK=LMA-dev-stack-2 WAV=/path/meeting.wav \
    BUNDLES=parakeet-tdt-v3-sortformer,parakeet-tdt-v3-titanet-small TRANSCRIBE=1 DRY_RUN=1

# The real run (long: run it in the background and report when it finishes).
AWS_PROFILE=default make asr-benchmark STACK=LMA-dev-stack-2 WAV=/path/meeting.wav \
    BUNDLES=parakeet-tdt-v3-sortformer,parakeet-tdt-v3-titanet-small TRANSCRIBE=1
```

Or call the CLI directly for the other options (`--reference`, `--no-diarize-system`,
`--diarize-mic`, `--keep-bundle`, `--template-bucket`, `--prices-file`):

```bash
AWS_PROFILE=default lma-load asr-benchmark -s LMA-dev-stack-2 --wav meeting.wav \
    --bundles parakeet-tdt-v3-titanet-small --transcribe --reference reference.txt
```

What each run does, in order (Transcribe first, then each bundle):
1. For a bundle: update the stack with its own template and only the `AsrDefaults`
   `ModelBundle` line changed, every parameter kept. Only the ASR nested stack
   changes; the MicroVM image rebuilds (~20-25 min). Skipped when the stack already
   runs that bundle.
2. Stream the WAV once at real time over the transcriber WebSocket as a temporary
   Cognito user, with `asrEngine` set to `microvm` or `transcribe`.
3. Wait for the MicroVM release (or the Transcribe stream to close), read the
   engine's log for CPU, memory and the bundle it ran, and price the run.
4. Read the final transcript back and save it.
At the end the original bundle is restored (unless `--keep-bundle`) and the
temporary user is deleted. The benchmark meetings are kept, named
`asr-bench-<run-id>-<config>`, so they can be opened in the UI.

Decision rules:
- Always dry-run first and tell the user the time estimate before the real run.
- Use a dev stack only; the tool refuses stack names containing `prod`.
- Ask the user for the recording if none is given. It must be **stereo 16-bit
  WAV** (channel 0 meeting audio, channel 1 microphone), ideally 20-30 minutes
  with two or more speakers. A Stream Audio or desktop app recording from the
  stack's recordings bucket (`lma-audio-recordings/<id>.wav`) works as-is.
- Include `TRANSCRIBE=1` unless the user says not to: it is the cost baseline and,
  without a reference transcript, the quality baseline.
- Bundle ids come from `lma-asr-microvm-stack/source/catalog.json`.
- Do not run other meetings on the stack during a benchmark: they would share the
  MicroVM image being switched.

## Reading the results

Written to `results/<run-id>/`: `asr-benchmark.md` (the table, also printed),
`asr-benchmark.json` (everything), `transcript-<config>.txt` per run, and the plan.

| Column | Meaning |
|---|---|
| Audio / MicroVM | Audio streamed; MicroVM lifetime from launch request to release |
| Avg / Peak cores | From the engine's per-minute `engine load` log line |
| Cost, $/audio h | MicroVM: memory × lifetime + vCPU-seconds at the larger of the baseline (memory ÷ 2 GiB, 4 vCPU at 8 GiB) and the cores in use. Transcribe: streamed seconds, 15 s minimum per request, a stereo stream billed once |
| Transcribe $ | What Transcribe would have cost for the same audio |
| Rows, Speakers | Final transcript rows; distinct speaker labels per channel |
| WER / vs Transcribe | Word error rate against `--reference`, or, without one, word-level disagreement with the Transcribe run (not an accuracy figure) |

Prices come from the AWS Price List API, or the deployment's default price table
(`CallPricing.json`) when the API is unreachable. Cost Explorer shows the day's
actual usage about a day later; earlier runs agreed within 1%.

Timing: about 25 min per bundle switch plus the recording's length plus ~3 min per
run, and one more switch to restore. Cost: a 27-minute recording costs $0.25-0.55 per
run (two-pass bundles are the most expensive).

## If something goes wrong

- `the meeting fell back to Amazon Transcribe`: the MicroVM did not start; check
  `/aws/lambda-microvms/<stack>-asr-logs` (`-asr` before v0.4.0) and the launcher.
- `engine reported bundle X, expected Y`: the image rebuild did not take effect.
- `the transcript shrank while it was read`: transcript deletion after the summary
  is on (Configuration → Retention); turn it off for the benchmark.
- A failed or interrupted run leaves the stack on the last bundle; run again with
  only the original bundle, or `make asr-benchmark … BUNDLES=<original>` with a
  short WAV, to restore it.

Unit tests: `make test-load-simulator` (no AWS).
