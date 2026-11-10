/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Troubleshooting page and the Diagnose button against a mocked GraphQL
 * client.
 *
 * What is asserted: the page asks the question typed or chosen and shows the
 * answer as sanitized Markdown once the job completes; a user outside the Admin
 * group gets an explanation rather than the form; the Diagnose button is shown
 * to Admins only, starts a diagnosis of its resource without a question,
 * reopening it shows the same diagnosis rather than paying for another, and a
 * refused or failed diagnosis says so.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import TroubleshootingPage, { STARTER_QUESTIONS, TroubleshootingContent } from './TroubleshootingPage';
import DiagnoseButton from './DiagnoseButton';
import TroubleshootingAnswer from './TroubleshootingAnswer';

const { graphqlMock, groupsMock } = vi.hoisted(() => ({
  graphqlMock: vi.fn(),
  groupsMock: vi.fn(),
}));
vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));
vi.mock('../../hooks/use-user-groups', () => ({ default: () => groupsMock() }));

const ANSWER = [
  '## Root Cause',
  '**Confidence:** HIGH',
  'The meeting platform refused the join.',
  '',
  '## Recommendations',
  '1. Check the meeting passcode.',
  '',
  '<details><summary><strong>Evidence</strong></summary>',
  '',
  '`VP exit: JOIN_REFUSED`',
  '',
  '</details>',
].join('\n');

const job = (overrides = {}) => ({
  JobId: 'job-1',
  Status: 'PENDING',
  Question: 'q',
  CallId: null,
  VirtualParticipantId: null,
  Answer: null,
  Error: null,
  Truncated: null,
  CreatedAt: new Date().toISOString(),
  UpdatedAt: new Date().toISOString(),
  ...overrides,
});

/** Answers start with a PENDING job and every read with the next of `reads`. */
const scripted = (started, reads) => {
  const queue = [...reads];
  graphqlMock.mockImplementation(async ({ query }) => {
    if (query.includes('startTroubleshooting')) return { data: { startTroubleshooting: started } };
    const next = queue.length > 1 ? queue.shift() : queue[0];
    return { data: { getTroubleshootingJob: next } };
  });
};

const startCalls = () => graphqlMock.mock.calls.filter(([request]) => request.query.includes('startTroubleshooting'));

beforeEach(() => {
  graphqlMock.mockReset();
  groupsMock.mockReset();
  groupsMock.mockReturnValue({ isAdmin: true, isLoading: false, userGroups: ['Admin'] });
});

describe('Troubleshooting page', () => {
  it('offers starter questions and asks the one chosen', async () => {
    scripted(job({ Question: STARTER_QUESTIONS[1] }), [
      job({ Status: 'RUNNING' }),
      job({ Status: 'COMPLETED', Answer: ANSWER }),
    ]);
    render(<TroubleshootingContent pollIntervalMs={5} />);

    fireEvent.click(screen.getByRole('button', { name: STARTER_QUESTIONS[1] }));

    await waitFor(() => expect(screen.getByTestId('troubleshooting-answer')).toBeInTheDocument());
    expect(startCalls()[0][0].variables).toEqual({ input: { Question: STARTER_QUESTIONS[1] } });
    const answer = screen.getByTestId('troubleshooting-answer');
    expect(within(answer).getByRole('heading', { name: 'Root Cause' })).toBeInTheDocument();
    expect(answer.querySelector('details summary')).toHaveTextContent('Evidence');
    // The starters are for an empty page.
    expect(screen.queryByRole('button', { name: STARTER_QUESTIONS[0] })).not.toBeInTheDocument();
  });

  it('sends a typed question with the scope it was given', async () => {
    scripted(job(), [job({ Status: 'COMPLETED', Answer: ANSWER })]);
    render(<TroubleshootingContent pollIntervalMs={5} />);

    fireEvent.change(screen.getByLabelText('Question').querySelector('textarea') || screen.getByLabelText('Question'), {
      target: { value: 'Why did it leave early?' },
    });
    fireEvent.click(screen.getByText('Scope (optional)'));
    const vpInput = screen.getByLabelText('Virtual Participant ID');
    fireEvent.change(vpInput.querySelector('input') || vpInput, { target: { value: 'vp-9' } });
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));

    await waitFor(() => expect(startCalls()).toHaveLength(1));
    expect(startCalls()[0][0].variables).toEqual({
      input: { Question: 'Why did it leave early?', VirtualParticipantId: 'vp-9' },
    });
    await waitFor(() => expect(screen.getByTestId('troubleshooting-answer')).toBeInTheDocument());
  });

  it('shows why a diagnosis failed', async () => {
    scripted(job(), [job({ Status: 'FAILED', Error: 'The diagnosis could not be completed.' })]);
    render(<TroubleshootingContent pollIntervalMs={5} />);
    fireEvent.click(screen.getByRole('button', { name: STARTER_QUESTIONS[0] }));
    await waitFor(() => expect(screen.getByText('The diagnosis could not be completed.')).toBeInTheDocument());
  });

  it('explains the page to a user outside the Admin group instead of showing the form', () => {
    groupsMock.mockReturnValue({ isAdmin: false, isLoading: false, userGroups: [] });
    render(<TroubleshootingPage />);
    expect(screen.getByText('Access denied')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Ask' })).not.toBeInTheDocument();
    expect(graphqlMock).not.toHaveBeenCalled();
  });
});

describe('Diagnose button', () => {
  it('starts a diagnosis of its meeting, with no question, when opened', async () => {
    scripted(job({ CallId: 'meeting-1' }), [job({ Status: 'COMPLETED', Answer: ANSWER, CallId: 'meeting-1' })]);
    render(<DiagnoseButton callId="meeting-1" resourceLabel="Weekly sync" pollIntervalMs={5} />);

    fireEvent.click(screen.getByRole('button', { name: 'Diagnose' }));

    await waitFor(() => expect(screen.getByTestId('troubleshooting-answer')).toBeInTheDocument());
    expect(startCalls()).toHaveLength(1);
    expect(startCalls()[0][0].variables).toEqual({ input: { CallId: 'meeting-1' } });
    expect(screen.getByText('Diagnose Weekly sync')).toBeInTheDocument();
  });

  it('shows the same diagnosis when reopened, and starts another only on Run again', async () => {
    scripted(job({ VirtualParticipantId: 'vp-1' }), [job({ Status: 'COMPLETED', Answer: ANSWER })]);
    render(<DiagnoseButton vpId="vp-1" pollIntervalMs={5} />);

    fireEvent.click(screen.getByRole('button', { name: 'Diagnose' }));
    await waitFor(() => expect(screen.getByTestId('troubleshooting-answer')).toBeInTheDocument());
    expect(startCalls()[0][0].variables).toEqual({ input: { VirtualParticipantId: 'vp-1' } });

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    fireEvent.click(screen.getByRole('button', { name: 'Diagnose' }));
    await waitFor(() => expect(screen.getByTestId('troubleshooting-answer')).toBeInTheDocument());
    expect(startCalls()).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'Run again' }));
    await waitFor(() => expect(startCalls()).toHaveLength(2));
  });

  it('shows a refusal from the resolver', async () => {
    graphqlMock.mockRejectedValue({
      errors: [{ message: 'Unauthorized: only members of the Admin group can run a diagnosis' }],
    });
    render(<DiagnoseButton callId="meeting-1" pollIntervalMs={5} />);
    fireEvent.click(screen.getByRole('button', { name: 'Diagnose' }));
    await waitFor(() =>
      expect(screen.getByText('Unauthorized: only members of the Admin group can run a diagnosis')).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: 'Run again' })).toBeInTheDocument();
  });

  it('is not shown to a user outside the Admin group', () => {
    groupsMock.mockReturnValue({ isAdmin: false, isLoading: false, userGroups: [] });
    render(<DiagnoseButton callId="meeting-1" />);
    expect(screen.queryByRole('button', { name: 'Diagnose' })).not.toBeInTheDocument();
    expect(graphqlMock).not.toHaveBeenCalled();
  });

  it('is not shown until the session says the user is an Admin', () => {
    groupsMock.mockReturnValue({ isAdmin: false, isLoading: true, userGroups: [] });
    render(<DiagnoseButton vpId="vp-1" />);
    expect(screen.queryByRole('button', { name: 'Diagnose' })).not.toBeInTheDocument();
  });

  it('is disabled with no resource to diagnose', () => {
    render(<DiagnoseButton />);
    expect(screen.getByRole('button', { name: 'Diagnose' })).toBeDisabled();
  });
});

describe('Troubleshooting answer', () => {
  it('renders model output through the sanitizing chain', () => {
    const withMarkup = `${ANSWER}\n\n<script>window.ran = true</script><img src="x" onerror="window.ran = true">`;
    const { container } = render(<TroubleshootingAnswer job={job({ Status: 'COMPLETED', Answer: withMarkup })} />);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('[onerror]')).toBeNull();
    expect(container.querySelector('details')).not.toBeNull();
  });

  it('says when the agent stopped at one of its limits', () => {
    render(<TroubleshootingAnswer job={job({ Status: 'COMPLETED', Answer: ANSWER, Truncated: true })} />);
    expect(screen.getByText(/reached one of its limits/)).toBeInTheDocument();
  });

  it('shows progress while the diagnosis runs', () => {
    render(<TroubleshootingAnswer job={job({ Status: 'RUNNING' })} />);
    expect(screen.getByText(/one to four minutes/)).toBeInTheDocument();
    expect(screen.getByTestId('troubleshooting-elapsed')).toBeInTheDocument();
  });
});
