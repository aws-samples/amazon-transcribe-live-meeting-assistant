# Load-Simulator Fixtures

Shipped audio and transcript fixtures used by the drivers.

The drivers (`upload`, `websocket`) pick a default WAV in this order:

1. **`stereo-demo-call.wav`** — real two-speaker call recording. Stereo
   PCM-16, ~5 minutes. ch_0 = caller, ch_1 = agent. The LMA WSS server
   enables Transcribe `ChannelIdentification` on stereo streams, so this
   fixture produces meaningful transcripts and downstream summaries.
   Use this for end-to-end functional tests / demos.
2. **`stereo-16k-30s.wav`** — 30-second synthetic two-tone clip
   (220 Hz / 330 Hz, alternating channels). Generated with
   ``python -m lma_load.fixtures.generate``. Useful **only** as a
   connectivity / quota-ceiling smoke test; will not produce a usable
   transcript or summary because there is no speech.

Override per-run with ``--wav <path>``. The driver loops the file if
``--duration`` is longer than the clip.

If neither default fixture is present in the package, the driver raises
a friendly error pointing you at ``--wav``. To re-generate the synthetic
fallback after cloning::

    python -m lma_load.fixtures.generate

