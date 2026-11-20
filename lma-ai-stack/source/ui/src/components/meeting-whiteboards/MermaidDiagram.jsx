/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/prop-types */
import React, { useEffect, useId, useState } from 'react';
import { Box } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import { mermaidProblem } from './whiteboard-model';

const logger = new ConsoleLogger('MermaidDiagram');

let mermaidPromise = null;

/**
 * Mermaid and DOMPurify, loaded the first time a diagram is shown, so a page
 * without one never downloads them. Strict mode: labels are escaped and the
 * diagram is not interactive. The server accepts only the subset
 * rules.check_mermaid allows, and the drawn SVG is sanitized again with
 * DOMPurify's SVG profile before it is inserted.
 */
export const loadMermaid = () => {
  if (!mermaidPromise) {
    mermaidPromise = Promise.all([import('mermaid'), import('dompurify')])
      .then(([{ default: mermaid }, { default: DOMPurify }]) => {
        mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
        return {
          render: async (id, source) => {
            const { svg } = await mermaid.render(id, source);
            return DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } });
          },
        };
      })
      .catch((error) => {
        mermaidPromise = null;
        throw error;
      });
  }
  return mermaidPromise;
};

const Source = ({ text, note }) => (
  <div>
    {note && (
      <Box variant="small" color="text-status-inactive">
        {note}
      </Box>
    )}
    <pre className="whiteboard-diagram-source">
      <code>{text}</code>
    </pre>
  </div>
);

/** A diagram panel's content, drawn; its source, when it cannot be drawn. */
const MermaidDiagram = ({ text }) => {
  const id = `wb-diagram-${useId().replace(/[^A-Za-z0-9_-]/g, '')}`;
  const [svg, setSvg] = useState(null);
  const [failed, setFailed] = useState(false);
  const source = (text || '').trim();
  const problem = source ? mermaidProblem(source) : null;

  useEffect(() => {
    let current = true;
    setSvg(null);
    setFailed(false);
    if (!source || problem) return undefined;
    loadMermaid()
      .then((renderer) => renderer.render(id, source))
      .then((sanitized) => {
        if (!current) return;
        if (sanitized) setSvg(sanitized);
        else setFailed(true);
      })
      .catch((error) => {
        logger.debug('diagram not drawn', error);
        if (current) setFailed(true);
      });
    return () => {
      current = false;
    };
  }, [id, source, problem]);

  if (!source) {
    return <Box color="text-status-inactive">No diagram yet.</Box>;
  }
  if (problem || failed) {
    return <Source text={source} note={problem || 'This diagram could not be drawn; its source is shown.'} />;
  }
  if (!svg) {
    return <Box color="text-status-inactive">Drawing the diagram…</Box>;
  }
  // Mermaid's own output in strict mode, sanitized with DOMPurify (loadMermaid).
  const markup = { __html: svg };
  return (
    <div
      className="whiteboard-diagram"
      data-testid="whiteboard-diagram"
      // eslint-disable-next-line react/no-danger
      dangerouslySetInnerHTML={markup}
    />
  );
};

export default MermaidDiagram;
