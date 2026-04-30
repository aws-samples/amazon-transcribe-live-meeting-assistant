/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedSelectAudio — Embeddable audio page that shows the Stream/Upload
 * mode switcher at the top. Renders the shared <StreamAudio /> component
 * with the Tiles mode selector visible.
 */
import React, { useEffect, useRef } from 'react';
import PropTypes from 'prop-types';

import StreamAudio from '../stream-audio/StreamAudio';

const EmbedSelectAudio = ({ sendToParent }) => {
  const sentLoadedRef = useRef(false);

  useEffect(() => {
    if (!sentLoadedRef.current && sendToParent) {
      sentLoadedRef.current = true;
      sendToParent({ type: 'LMA_EMBED_LOADED', component: 'select-audio' });
    }
  }, [sendToParent]);

  return (
    <div className="embed-select-audio" style={{ padding: '1rem' }}>
      <StreamAudio mode="select" />
    </div>
  );
};

EmbedSelectAudio.propTypes = {
  sendToParent: PropTypes.func,
};

EmbedSelectAudio.defaultProps = {
  sendToParent: null,
};

export default EmbedSelectAudio;
