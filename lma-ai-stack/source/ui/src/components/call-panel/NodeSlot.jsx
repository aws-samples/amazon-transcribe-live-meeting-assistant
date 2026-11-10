/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Keeps a component mounted while the place it is shown comes and goes.
 *
 * The meeting page's Screen share tab exists only once the key frame strip has
 * loaded a frame, and it is the strip that loads them -- so the strip cannot
 * live inside the tab. It is rendered through a portal into a node of its own
 * (useDetachedNode), and the tab's content is a slot (NodeSlot) that the node is
 * moved into while the tab exists. Moving the node does not remount what is in
 * it: the strip keeps its frames, its paging and its live subscription whether
 * the tab is there, shown or hidden.
 */
import React, { useLayoutEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';

/** A node of its own, for one component's lifetime, to portal into. */
export const useDetachedNode = () => {
  const [node] = useState(() => (typeof document === 'undefined' ? null : document.createElement('div')));
  return node;
};

/** Shows `node` here for as long as this slot is mounted. */
export const NodeSlot = ({ node = null }) => {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const host = ref.current;
    if (!host || !node) return undefined;
    host.appendChild(node);
    return () => {
      if (node.parentNode === host) host.removeChild(node);
    };
  }, [node]);
  return <div ref={ref} />;
};

NodeSlot.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  node: PropTypes.object,
};
