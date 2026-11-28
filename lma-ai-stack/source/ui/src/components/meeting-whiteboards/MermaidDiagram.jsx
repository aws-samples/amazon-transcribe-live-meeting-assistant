/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/prop-types */
import React, { useEffect, useId, useRef, useState } from 'react';
import { Box } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import { mermaidProblem } from './whiteboard-model';

const logger = new ConsoleLogger('MermaidDiagram');

let mermaidPromise = null;

/**
 * Mermaid and DOMPurify, loaded the first time a diagram is shown, so a page
 * without one never downloads them. Strict mode: labels are escaped and the
 * diagram is not interactive. Labels are SVG text rather than HTML, so the
 * sanitizing below keeps them. The server accepts only the subset
 * rules.check_mermaid allows, and the drawn SVG is sanitized again with
 * DOMPurify's SVG profile before it is inserted.
 */
export const loadMermaid = () => {
  if (!mermaidPromise) {
    mermaidPromise = Promise.all([import('mermaid'), import('dompurify')])
      .then(([{ default: mermaid }, { default: DOMPurify }]) => {
        // Labels are drawn as SVG text: by default flowcharts and mind maps put
        // them in HTML inside <foreignObject>, which DOMPurify's SVG profile
        // removes, and the diagram is then drawn with no labels at all.
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          htmlLabels: false,
          flowchart: { htmlLabels: false },
        });
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

/**
 * The source as it is drawn, with LMA's own directive added after the source was
 * checked (which may hold none): Mermaid's dark theme on a dark page, and a Gantt
 * chart drawn at the width it has on the page rather than scaled down from the
 * page's width.
 */
export const sourceToDraw = (source, width, dark = false) => {
  const init = {};
  if (dark) init.theme = 'dark';
  if (/^\s*(%%[^\n]*\n\s*)*gantt\b/.test(source)) {
    init.gantt = {
      useWidth: Math.max(480, Math.round(width || 0) || 960),
      useMaxWidth: false,
      barHeight: 24,
      fontSize: 13,
      sectionFontSize: 13,
    };
  }
  return Object.keys(init).length ? `%%{init: ${JSON.stringify(init)}}%%\n${source}` : source;
};

const isDarkPage = () => typeof document !== 'undefined' && document.body.classList.contains('awsui-dark-mode');

/** Widths are followed in steps of this many pixels, so a resize redraws a chart only now and then. */
const WIDTH_STEP = 40;

/**
 * The width of the element `ref` holds, in WIDTH_STEP steps, or 0 while it is not
 * laid out (a tab that is not shown); and whether the page is dark. Each change
 * redraws a diagram that depends on it.
 */
const useDrawingContext = (ref, follow) => {
  const [width, setWidth] = useState(0);
  const [dark, setDark] = useState(isDarkPage);
  useEffect(() => {
    if (!follow || typeof ResizeObserver === 'undefined' || !ref.current) return undefined;
    const observer = new ResizeObserver(([entry]) => {
      const measured = Math.floor((entry?.contentRect?.width || 0) / WIDTH_STEP) * WIDTH_STEP;
      setWidth(measured);
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [ref, follow]);
  useEffect(() => {
    if (typeof MutationObserver === 'undefined' || typeof document === 'undefined') return undefined;
    const observer = new MutationObserver(() => setDark(isDarkPage()));
    observer.observe(document.body, { attributes: true, attributeFilter: ['class'] });
    return () => observer.disconnect();
  }, []);
  return { width, dark };
};

/** A diagram panel's content, drawn; its source, when it cannot be drawn. */
const MermaidDiagram = ({ text }) => {
  const id = `wb-diagram-${useId().replace(/[^A-Za-z0-9_-]/g, '')}`;
  const [svg, setSvg] = useState(null);
  const [failed, setFailed] = useState(false);
  const boxRef = useRef(null);
  const source = (text || '').trim();
  const problem = source ? mermaidProblem(source) : null;
  // Only a Gantt chart is drawn at a width of its own; others are drawn once and scaled.
  const isGantt = sourceToDraw(source, 1) !== source;
  const { width, dark } = useDrawingContext(boxRef, isGantt);

  useEffect(() => {
    let current = true;
    setSvg(null);
    setFailed(false);
    if (!source || problem) return undefined;
    // A Gantt chart on a tab that is not shown waits until it is laid out.
    if (isGantt && !width && typeof ResizeObserver !== 'undefined') return undefined;
    loadMermaid()
      .then((renderer) => renderer.render(id, sourceToDraw(source, width, dark)))
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
  }, [id, source, problem, isGantt, width, dark]);

  let shown;
  if (!source) {
    shown = <Box color="text-status-inactive">No diagram yet.</Box>;
  } else if (problem || failed) {
    shown = <Source text={source} note={problem || 'This diagram could not be drawn; its source is shown.'} />;
  } else if (!svg) {
    shown = <Box color="text-status-inactive">Drawing the diagram…</Box>;
  } else {
    // Mermaid's own output in strict mode, sanitized with DOMPurify (loadMermaid).
    const markup = { __html: svg };
    shown = (
      <div
        className="whiteboard-diagram"
        data-testid="whiteboard-diagram"
        // eslint-disable-next-line react/no-danger
        dangerouslySetInnerHTML={markup}
      />
    );
  }
  // Measured whatever is shown, so a redrawn chart gets the panel's width.
  return <div ref={boxRef}>{shown}</div>;
};

export default MermaidDiagram;
