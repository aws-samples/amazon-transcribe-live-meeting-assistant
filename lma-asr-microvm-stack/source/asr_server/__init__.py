# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""ASR server that runs inside a Firecracker MicroVM.

Serves a native WebSocket on :8080, wraps sherpa-onnx ``OnlineRecognizer`` for
frame-synchronous streaming recognition, gates audio with Silero VAD, applies
endpointing rules, and adapts sherpa events to the wire protocol defined in
``design.md`` §5.
"""
