"""ASR server that runs inside a Firecracker MicroVM.

Serves a native WebSocket on :8080, wraps sherpa-onnx ``OnlineRecognizer`` for
frame-synchronous streaming recognition, gates audio with Silero VAD, applies
endpointing rules, and adapts sherpa events to the wire protocol defined in
``design.md`` §5.
"""
