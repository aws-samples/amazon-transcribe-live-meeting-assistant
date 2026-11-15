# AMI Meeting Corpus ground truth for the ASR benchmark

The reference transcripts in this directory are derived from the
[AMI Meeting Corpus](https://groups.inf.ed.ac.uk/ami/corpus/), which is released under
the [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)
licence (CC BY 4.0). The audio is not stored here: the benchmark downloads each meeting's
headset recordings from the AMI mirror on first use and checks them against the SHA-256 in
`manifest.json`.

## Attribution

AMI Meeting Corpus, © the AMI Consortium (University of Edinburgh, Idiap Research
Institute, TNO, Brno University of Technology and others), CC BY 4.0.

> Jean Carletta et al. *The AMI Meeting Corpus: A Pre-Announcement.* In Machine Learning for
> Multimodal Interaction (MLMI 2005), Lecture Notes in Computer Science 3869, Springer, 2006.

## Meetings

| Meeting | Length | Speakers | Microphone channel (host) | Audio | Source |
|---|---|---|---|---|---|
| `IS1009c` | 30.3 min | 4 | A, the project manager | clean | AMI test set, recorded at Idiap |
| `IS1009d` | 32.4 min | 4 | A, the project manager | clean | AMI test set, recorded at Idiap |
| `ES2004a` | 17.5 min | 4 | B, the project manager | **noisy** | AMI test set, recorded in Edinburgh |

`IS1009c` and `IS1009d` are the clean-audio benchmark: close-talk headsets at normal levels.
`ES2004a`'s headsets were recorded at a low level (about a fifth of the Idiap meetings'), so
bringing its speakers to speech level also raises the noise floor. Use it to test noise
robustness, not to compare engines on clean audio: a detector or recogniser with its own
noise handling has an advantage there.

Use them with `lma load asr-benchmark --dataset ami/IS1009c` or
`make asr-benchmark DATASET=ami/IS1009c`.

## Changes made to the original

- **Transcripts.** `<meeting>.stm` is generated from the manual word annotations, release
  1.6.2 (`ami_public_manual_1.6.2.zip`, `words/<meeting>.<speaker>.words.xml`). A speaker's
  words are joined into one utterance until a gap longer than 1 s; punctuation tokens are
  attached to the preceding word; vocal sounds and other non-word annotations are left out.
  The host's utterances are on channel 1 (the LMA microphone), everyone else's on channel 0
  (the meeting audio).
- **Audio.** The stereo recording is built from the four individual headset recordings
  (`<meeting>.Headset-<n>.wav`, 16 kHz). Each headset is kept only within 0.3 s of its own
  wearer's utterances, which removes what it picks up of the other people in the room, so
  that each participant sounds like a separate remote caller. Each speaker is brought to the
  same speech level (with a gain cap that clips at most 0.01% of samples). Channel 0 is the
  sum of the non-host speakers, channel 1 the host, both at 90%.

`ami.py` in the parent directory does both steps (`package` for the transcripts and the
manifest, `build` for the audio), so the files here can be regenerated from the original
corpus.
