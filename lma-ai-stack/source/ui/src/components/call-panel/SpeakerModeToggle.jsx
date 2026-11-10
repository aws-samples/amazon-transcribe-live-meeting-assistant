/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The one-person / several-people choice for one platform name, as a single
 * icon that switches between the two: one person (the user-profile icon) or
 * several people (the group icon). It takes the room of one small icon beside
 * a name's chips above the transcript and beside the name in the Speakers
 * panel.
 *
 * A toggle button: pressed means several people. Its accessible name says
 * which name it is for, `aria-pressed` says which way it is, and the space and
 * enter keys press it, as for any button. The tooltip -- shown on hover and on
 * keyboard focus, and closed with Escape -- says how the name is shown, why
 * (describeSpeakerMode), and what a click does; the same text is the button's
 * accessible description, so a screen-reader user hears it too. For a viewer
 * who may not change it (`onSetMode` absent) the button is disabled and the
 * tooltip leaves out the click.
 */
import React, { useCallback, useId, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { Box, ToggleButton, Tooltip } from '@cloudscape-design/components';

import { ONE_PERSON, SEVERAL_PEOPLE, speakerModeTip } from '../common/speaker-modes';

const SpeakerModeToggle = ({ group, onSetMode = undefined }) => {
  const anchorRef = useRef(null);
  const descriptionId = useId();
  const [tipOpen, setTipOpen] = useState(false);
  const several = group.mode === SEVERAL_PEOPLE;
  const tip = speakerModeTip(group, Boolean(onSetMode));
  const description = [tip.mode, tip.reason, tip.action].filter(Boolean).join(' ');
  const getTrack = useCallback(() => anchorRef.current, []);
  const open = () => setTipOpen(true);
  const close = () => setTipOpen(false);

  return (
    // The wrapper takes the hover, because a disabled button does not.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <span
      ref={anchorRef}
      className="speaker-mode-toggle"
      onMouseEnter={open}
      onMouseLeave={close}
      onFocus={open}
      onBlur={close}
      data-testid="speaker-mode-toggle"
    >
      <ToggleButton
        variant="icon"
        iconName="user-profile"
        pressedIconName="group"
        pressed={several}
        ariaLabel={`Show ${group.base} as several people`}
        ariaDescribedby={descriptionId}
        disabled={!onSetMode}
        onChange={({ detail }) => onSetMode?.(group.key, detail.pressed ? SEVERAL_PEOPLE : ONE_PERSON)}
      />
      <span id={descriptionId} hidden>
        {description}
      </span>
      {tipOpen && (
        <Tooltip
          getTrack={getTrack}
          position="top"
          onEscape={close}
          content={
            <Box fontSize="body-s">
              <Box fontSize="body-s" fontWeight="bold">
                {tip.mode}
              </Box>
              <Box fontSize="body-s">{tip.reason}</Box>
              {tip.action && (
                <Box fontSize="body-s" fontWeight="bold">
                  {tip.action}
                </Box>
              )}
            </Box>
          }
        />
      )}
    </span>
  );
};

SpeakerModeToggle.propTypes = {
  /** A speakerGroups group: `key`, `base`, `mode`, and what describeSpeakerMode reads. */
  group: PropTypes.shape({
    key: PropTypes.string.isRequired,
    base: PropTypes.string.isRequired,
    mode: PropTypes.string,
  }).isRequired,
  /** Called with the group key and the mode chosen; absent when the reader may not choose. */
  onSetMode: PropTypes.func,
};

export default SpeakerModeToggle;
