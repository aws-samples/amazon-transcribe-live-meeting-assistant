/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The One person / Several people choice for one platform name, as a small menu.
 *
 * A menu rather than a two-option segmented control so it takes the room of one
 * button beside a name's chips: `compact` shows only an icon (one person, or a
 * group), for the row of chips above the transcript; otherwise the button reads
 * the current choice, for the Speakers panel. Either way the button's
 * accessible name says which name it is for and how that name is shown, and
 * the menu marks the current choice, so a screen-reader or keyboard user gets
 * the same information as the icon. The reason for the current choice
 * (describeSpeakerMode) is the button's tooltip.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { ButtonDropdown } from '@cloudscape-design/components';

import { describeSpeakerMode, ONE_PERSON, SEVERAL_PEOPLE } from '../common/speaker-modes';

const MODE_TEXT = { [ONE_PERSON]: 'One person', [SEVERAL_PEOPLE]: 'Several people' };

const SpeakerModeMenu = ({ group, onSetMode = undefined, compact = false }) => {
  const current = MODE_TEXT[group.mode] ? group.mode : ONE_PERSON;
  return (
    <span title={describeSpeakerMode(group)} className="speaker-mode-menu">
      <ButtonDropdown
        variant={compact ? 'inline-icon' : 'normal'}
        iconName={current === SEVERAL_PEOPLE ? 'group' : 'user-profile'}
        ariaLabel={`How the voices under ${group.base} are shown: ${MODE_TEXT[current]}`}
        disabled={!onSetMode}
        expandToViewport
        items={[ONE_PERSON, SEVERAL_PEOPLE].map((mode) => ({
          id: mode,
          text: MODE_TEXT[mode],
          itemType: 'checkbox',
          checked: mode === current,
        }))}
        onItemClick={({ detail }) => {
          if (detail.id !== current) onSetMode?.(group.key, detail.id);
        }}
      >
        {compact ? undefined : MODE_TEXT[current]}
      </ButtonDropdown>
    </span>
  );
};

SpeakerModeMenu.propTypes = {
  /** A speakerGroups group: `key`, `base`, `mode`, and what describeSpeakerMode reads. */
  group: PropTypes.shape({
    key: PropTypes.string.isRequired,
    base: PropTypes.string.isRequired,
    mode: PropTypes.string,
  }).isRequired,
  /** Called with the group key and the mode chosen; absent when the reader may not choose. */
  onSetMode: PropTypes.func,
  /** An icon alone, for the row of chips above the transcript. */
  compact: PropTypes.bool,
};

export default SpeakerModeMenu;
