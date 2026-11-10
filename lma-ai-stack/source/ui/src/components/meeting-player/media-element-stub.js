/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * For tests: jsdom's audio and video elements load nothing and cannot play.
 * This gives them a position, a length and a play state that the test sets,
 * and fires the events a browser would. Imported by tests only.
 */
/* eslint-disable no-underscore-dangle */
import { fireEvent } from '@testing-library/react';

export const installMediaElementStub = () => {
  const proto = window.HTMLMediaElement.prototype;
  const field = (name, key, fallback, onSet) =>
    Object.defineProperty(proto, name, {
      configurable: true,
      get() {
        return this[key] ?? fallback;
      },
      set(value) {
        this[key] = value;
        if (onSet) onSet(this);
      },
    });
  field('readyState', '__readyState', 0);
  field('duration', '__duration', NaN);
  field('paused', '__paused', true);
  field('currentTime', '__currentTime', 0, (media) => fireEvent(media, new Event('seeked')));
  proto.play = function play() {
    this.__paused = false;
    fireEvent(this, new Event('play'));
    return Promise.resolve();
  };
  proto.pause = function pause() {
    this.__paused = true;
    fireEvent(this, new Event('pause'));
  };
  proto.load = () => undefined;
};

/** The element's metadata arrives: it can now be positioned. */
export const loadMedia = (media, duration) => {
  /* eslint-disable no-param-reassign */
  media.__readyState = 1;
  media.__duration = duration;
  /* eslint-enable no-param-reassign */
  fireEvent(media, new Event('loadedmetadata'));
};
