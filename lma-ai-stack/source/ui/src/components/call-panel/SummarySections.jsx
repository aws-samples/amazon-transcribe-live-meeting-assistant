/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting summary, a section at a time.
 *
 * A summary is made of sections (SUMMARY, DETAILS, ACTIONS, SCREEN SHARE
 * SUMMARY and so on, one per prompt template), and some run to several
 * screens. Each is shown as a section that opens and closes, under a line of
 * links that open a section and move to it, with "Expand all" and "Collapse
 * all" beside them. The SUMMARY section starts open and the others closed
 * (the first section starts open when none is called SUMMARY); whatever the
 * viewer opens or closes is remembered, per section name, for every meeting
 * (localStorage, under SECTIONS_STORAGE_KEY).
 *
 * A closed section's text is not rendered, so the pictures of the shared
 * screen in it are read (and their links signed) only once it is opened.
 */
import React, { useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, ExpandableSection, SpaceBetween, TextContent } from '@cloudscape-design/components';

import SummaryMarkdown from '../summary-markdown/SummaryMarkdown';
import useLocalStorage from '../common/local-storage';

export const SECTIONS_STORAGE_KEY = 'lma-summary-sections';

/** A section's name as the remembered choices are keyed: case and spacing aside. */
const nameOf = (title) =>
  String(title || '')
    .trim()
    .toUpperCase();

/** Whether a section starts open, before the viewer has chosen: SUMMARY, or the first when none is. */
export const openByDefault = (sections, index) => {
  const hasSummary = sections.some((section) => nameOf(section.title) === 'SUMMARY');
  return hasSummary ? nameOf(sections[index].title) === 'SUMMARY' : index === 0;
};

const SummarySections = ({ callId = '', sections, keyframes = 'image', onKeyframeSelect = undefined }) => {
  const [remembered, setRemembered] = useLocalStorage(SECTIONS_STORAGE_KEY, {});
  const choices = remembered && typeof remembered === 'object' ? remembered : {};
  const isOpen = (index) => {
    const choice = choices[nameOf(sections[index].title)];
    return typeof choice === 'boolean' ? choice : openByDefault(sections, index);
  };
  const setOpen = (updates) => setRemembered({ ...choices, ...updates });

  // A section asked for from the links: moved to once it has opened.
  const sectionRefs = useRef({});
  const pendingScroll = useRef(null);
  useEffect(() => {
    const name = pendingScroll.current;
    if (name === null) return;
    pendingScroll.current = null;
    sectionRefs.current[name]?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  });

  if (!sections.length) {
    return <Box color="text-body-secondary">Not available</Box>;
  }

  // A hand-written summary can repeat a heading; each section still needs a key of its own.
  const seen = {};
  const keys = sections.map(({ title }) => {
    seen[title] = (seen[title] || 0) + 1;
    return seen[title] > 1 ? `${title} (${seen[title]})` : title;
  });
  const allOpen = sections.every((_section, index) => isOpen(index));
  const noneOpen = sections.every((_section, index) => !isOpen(index));
  const setAll = (open) => setOpen(Object.fromEntries(sections.map((section) => [nameOf(section.title), open])));
  const jumpTo = (section) => {
    pendingScroll.current = nameOf(section.title);
    setOpen({ [nameOf(section.title)]: true });
  };

  return (
    <SpaceBetween size="xs">
      {sections.length > 1 && (
        <div className="summary-toc">
          <nav aria-label="Summary sections">
            <ul className="summary-toc-list">
              {sections.map((section, index) => (
                <li key={keys[index]}>
                  <Button variant="inline-link" onClick={() => jumpTo(section)}>
                    {section.title}
                  </Button>
                </li>
              ))}
            </ul>
          </nav>
          <SpaceBetween size="xs" direction="horizontal">
            <Button variant="inline-link" disabled={allOpen} onClick={() => setAll(true)}>
              Expand all
            </Button>
            <Button variant="inline-link" disabled={noneOpen} onClick={() => setAll(false)}>
              Collapse all
            </Button>
          </SpaceBetween>
        </div>
      )}
      {sections.map((section, index) => {
        const open = isOpen(index);
        return (
          <div
            key={keys[index]}
            className="summary-section"
            ref={(node) => {
              sectionRefs.current[nameOf(section.title)] = node;
            }}
          >
            <ExpandableSection
              headerText={section.title}
              expanded={open}
              onChange={({ detail }) => setOpen({ [nameOf(section.title)]: detail.expanded })}
            >
              {open && (
                <TextContent color="gray">
                  <SummaryMarkdown callId={callId} keyframes={keyframes} onKeyframeSelect={onKeyframeSelect}>
                    {section.markdown}
                  </SummaryMarkdown>
                </TextContent>
              )}
            </ExpandableSection>
          </div>
        );
      })}
    </SpaceBetween>
  );
};

SummarySections.propTypes = {
  callId: PropTypes.string,
  sections: PropTypes.arrayOf(PropTypes.shape({ title: PropTypes.string, markdown: PropTypes.string })).isRequired,
  keyframes: PropTypes.oneOf(['image', 'link', 'text']),
  onKeyframeSelect: PropTypes.func,
};

export default SummarySections;
