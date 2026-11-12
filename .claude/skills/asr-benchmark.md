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

The default input is a **packaged ground-truth meeting**: a meeting from the AMI Meeting
Corpus (CC BY 4.0) with a human-made reference transcript in the repository
(`utilities/load-simulator/lma_load/asr_benchmark/datasets/ami/`). Its audio is downloaded
from the AMI mirror and built into a stereo recording on first use (cached in
`~/.cache/lma-asr-benchmark`), so every run hears identical audio and is scored for real
accuracy (WER) against the reference.

| Dataset | Length | Speakers | Audio |
|---|---|---|---|
| `ami/IS1009c` | 30.3 min | 4 (host on the microphone, 3 in the meeting audio) | clean |
| `ami/IS1009d` | 32.4 min | 4 | clean |
| `ami/ES2004a` | 17.5 min | 4 | noisy (low-level headsets) |

Compare engines on the **clean** meetings. `ami/ES2004a` is a noise-robustness test: its
headsets were recorded at a low level, so levelling raises the noise floor, and an engine
with its own noise handling (Amazon Transcribe) has an advantage there.

```bash
# Dry run first: prints the plan, the bundle switches and the expected time.
AWS_PROFILE=default make asr-benchmark STACK=LMA-dev-stack-2 DATASET=ami/IS1009c \
    BUNDLES=parakeet-tdt-v3-sortformer,parakeet-tdt-v3-titanet-small TRANSCRIBE=1 DRY_RUN=1

# The real run (long: run it detached, see below, and report when it finishes).
AWS_PROFILE=default make asr-benchmark STACK=LMA-dev-stack-2 DATASET=ami/IS1009c \
    BUNDLES=parakeet-tdt-v3-sortformer,parakeet-tdt-v3-titanet-small TRANSCRIBE=1
```

A recording of your own works too: `WAV=/path/meeting.wav`, optionally with
`REFERENCE=/path/meeting.stm`. Without a reference there is no WER column: agreement with
the Transcribe run is kept in `asr-benchmark.json` only, because it is not accuracy.

Or call the CLI directly for the other options (`--no-diarize-system`, `--diarize-mic`,
`--keep-bundle`, `--template-bucket`, `--prices-file`, `--cache-dir`):

```bash
AWS_PROFILE=default lma-load asr-benchmark -s LMA-dev-stack-2 --dataset ami/IS1009d \
    --bundles parakeet-tdt-v3-titanet-small --transcribe
```

**Run long benchmarks detached** (`setsid nohup make asr-benchmark … > log 2>&1 &`), so
they survive the session that started them. An interrupted run leaves the stack on the
bundle it was testing; the next run restores to whatever bundle was current when it
started, so switch back explicitly: `make asr-benchmark STACK=<stack>
SWITCH_ONLY=<original bundle>` switches without streaming anything.

**Reference transcripts (STM).** One line per utterance:
`<recording> <channel> <speaker> <start> <end> <words>`, channel `0` (meeting audio) or
`1` (microphone). Lines starting `;;` are comments. To add an AMI meeting, download its
headset WAVs and the manual annotations, then
`python -m lma_load.asr_benchmark.datasets.ami package <meeting> <host letter>
--annotations <dir> --headsets <dir>` writes the STM and its manifest entry.

What each run does, in order (Transcribe first, then each bundle):
1. For a bundle: update the stack with its own template and only the `AsrDefaults`
   `ModelBundle` line changed, every parameter kept. The ASR nested stack changes
   and the MicroVM image rebuilds (~20-25 min); the transcriber stack takes the new
   bundle id, name and languages, and the Virtual Participant stack the new languages.
   Skipped when the stack already runs that bundle. Before switching,
   each nested stack's deployed template is compared with the one its `TemplateURL`
   now points to; if `publish.sh` has been re-run to that location since the last
   deploy, the switch refuses rather than deploying that build as a side effect.
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
| Cost, used cores | The same run priced at only the core-seconds the engine measured (memory unchanged): what it would cost without the vCPU baseline |
| Transcribe $ | What Transcribe would have cost for the same audio |
| Rows, Speakers | Final transcript rows; distinct speaker labels per channel, "of N" against the reference's speaker count |
| WER meeting / microphone | Only with a reference. Word error rate per channel, with its split into swapped / missing / extra words, after normalisation (case, punctuation, fillers such as um and uh, okay/ok, numbers up to 999, -isation/-ization). Lower is better |

The packaged AMI meetings gate each headset to its wearer's annotated speech, so every
engine hears close-talk speech without background noise or crosstalk between turns.
That keeps the comparison fair, since all engines get the same audio, but absolute WERs,
insertions especially, read better than a real meeting's would.

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
- A failed or interrupted run leaves the stack on the last bundle. Restore it with
  `make asr-benchmark STACK=<stack> SWITCH_ONLY=<original bundle>`; the run's log names
  the bundle when its own restore fails. Rerunning with `BUNDLES=<original>` does not
  work: that run would restore to the wrong bundle at its end.
- `the published templates of … differ from what <stack> runs`: `publish.sh` was run
  again since the stack was last deployed. Deploy that build, or publish the deployed
  version again, then rerun.

Unit tests: `make test-load-simulator` (no AWS).
