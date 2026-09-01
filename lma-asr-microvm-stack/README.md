# On-demand ASR & Speaker Diarization (MicroVM)

This documentation has moved to [docs/microvm-asr.md](../docs/microvm-asr.md).

The ASR runtime under [`source/`](source/) is vendored from the
`aws-lambda-microvm-asr` prototype (streaming `sherpa-onnx` recognizer, online
diarization, and the WebSocket protocol). What is new here is the AWS integration:
the `asr_microvm` lifecycle hooks, the model catalog and build-time fetch, the
session launcher, and the CloudFormation stack.
