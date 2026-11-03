/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The names of a meeting transcript's two speech channels.
 *
 * REMOTE is everyone else in the meeting (the meeting audio); LOCAL is the local
 * side (the user's microphone, or a Virtual Participant's voice assistant).
 *
 * LMA before 0.4.0 wrote them as CALLER and AGENT. Meetings stored by those
 * versions keep the old names until they expire, so every value read from the
 * API goes through canonicalChannel, and everything below compares only the
 * current names.
 *
 * The Python twin is lma_channels in the configuration Lambda layer;
 * lma_channels/test_lma_channels.py holds the alias map below to it.
 */

export const REMOTE_CHANNEL = 'REMOTE';
export const LOCAL_CHANNEL = 'LOCAL';

/** The two speech channels, as written. */
export const SPEECH_CHANNELS = [REMOTE_CHANNEL, LOCAL_CHANNEL];

// Older spellings, still read. CUSTOMER is Amazon Transcribe's role name for the
// non-agent side of a call. One entry per line: the Python twin's test reads them.
const CHANNEL_ALIASES = {
  CALLER: REMOTE_CHANNEL,
  CUSTOMER: REMOTE_CHANNEL,
  AGENT: LOCAL_CHANNEL,
};

const own = (map, key) => typeof key === 'string' && Object.prototype.hasOwnProperty.call(map, key);

/** The current name of a channel; any other value is returned unchanged. */
export const canonicalChannel = (channel) => (own(CHANNEL_ALIASES, channel) ? CHANNEL_ALIASES[channel] : channel);

/** Whether a channel, under either spelling, is one of the two speech channels. */
export const isSpeechChannel = (channel) => SPEECH_CHANNELS.includes(canonicalChannel(channel));

// What the UI calls each channel where it names one: sentiment charts and
// columns, and a voice with no voice id in the Speakers panel. Not "Me": on a
// Virtual Participant meeting the LOCAL channel is the voice assistant.
const CHANNEL_LABELS = {
  [REMOTE_CHANNEL]: 'Remote',
  [LOCAL_CHANNEL]: 'Local',
};

/** The label shown for a channel; a channel without one is shown by name. */
export const channelLabel = (channel) => {
  const canonical = canonicalChannel(channel);
  return own(CHANNEL_LABELS, canonical) ? CHANNEL_LABELS[canonical] : canonical;
};

/** A speaker-identity key (`<channel>#<speaker>`) under the current channel name. */
export const canonicalSpeakerKey = (key) => {
  if (typeof key !== 'string') {
    return key;
  }
  const at = key.indexOf('#');
  if (at < 0) {
    return key;
  }
  const channel = key.slice(0, at);
  return own(CHANNEL_ALIASES, channel) ? `${CHANNEL_ALIASES[channel]}${key.slice(at)}` : key;
};

/**
 * A speaker-identity section re-keyed under the current channel names. With
 * valuesAreKeys the values are re-keyed too, as `merges` maps a key to a key.
 *
 * A meeting recorded across an upgrade can hold one voice under both spellings;
 * the entry already under the current name wins, because it was written later.
 */
export const canonicalKeyed = (section, valuesAreKeys = false) => {
  const out = {};
  const renamed = {};
  Object.entries(section ?? {}).forEach(([key, value]) => {
    const newValue = valuesAreKeys ? canonicalSpeakerKey(value) : value;
    const newKey = canonicalSpeakerKey(key);
    if (newKey === key) {
      out[key] = newValue;
    } else {
      renamed[newKey] = newValue;
    }
  });
  Object.entries(renamed).forEach(([key, value]) => {
    if (!own(out, key)) {
      out[key] = value;
    }
  });
  return out;
};

/**
 * A per-channel sentiment map ({ CALLER: ..., REMOTE: ... }) keyed by current
 * channel names. A meeting stored before 0.4.0 has only the older keys; a newer
 * one has only the current keys, and the older ones come back null.
 */
const canonicalByChannel = (byChannel) => {
  if (!byChannel || typeof byChannel !== 'object') {
    return byChannel;
  }
  const out = {};
  // Current names first, so that their values win.
  Object.entries(byChannel).forEach(([key, value]) => {
    if (canonicalChannel(key) === key) {
      out[key] = value;
    }
  });
  Object.entries(byChannel).forEach(([key, value]) => {
    const newKey = canonicalChannel(key);
    if (newKey !== key && (out[newKey] === undefined || out[newKey] === null)) {
      out[newKey] = value;
    }
  });
  return out;
};

/** A meeting's Sentiment aggregation with its per-channel maps keyed by current names. */
export const canonicalSentiment = (sentiment) => {
  if (!sentiment || typeof sentiment !== 'object') {
    return sentiment;
  }
  const quarter = sentiment.SentimentByPeriod?.QUARTER;
  return {
    ...sentiment,
    ...(sentiment.OverallSentiment ? { OverallSentiment: canonicalByChannel(sentiment.OverallSentiment) } : {}),
    ...(quarter ? { SentimentByPeriod: { ...sentiment.SentimentByPeriod, QUARTER: canonicalByChannel(quarter) } } : {}),
  };
};
