/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting page's selected tab.
 *
 * On the meeting's own page (`inUrl`) the tab is the `tab` query parameter, so
 * a link or a reload opens the same tab; changing it replaces the history
 * entry rather than adding one, so Back leaves the meeting instead of stepping
 * through the tabs that were looked at. Where the panel is shown inside another
 * page -- the meeting list's split panel, the embeddable components -- the tab
 * is held locally and the host page's URL is left alone.
 *
 * A requested tab the meeting does not have (yet: the Screen share tab appears
 * once the first key frames have loaded) shows the default tab, but the
 * request is kept, so the tab opens when it appears.
 */
import { useCallback, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { TAB_PARAM, resolveTab } from '../components/call-panel/meeting-tabs';

const useMeetingTab = ({ inUrl = false, isLive = false, hasKeyframes = false } = {}) => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [localTab, setLocalTab] = useState(null);
  const requested = inUrl ? searchParams.get(TAB_PARAM) : localTab;
  const activeTab = resolveTab(requested, { isLive, hasKeyframes });

  const setTab = useCallback(
    (tabId) => {
      if (!inUrl) {
        setLocalTab(tabId);
        return;
      }
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          next.set(TAB_PARAM, tabId);
          return next;
        },
        { replace: true },
      );
    },
    [inUrl, setSearchParams],
  );

  return [activeTab, setTab];
};

export default useMeetingTab;
