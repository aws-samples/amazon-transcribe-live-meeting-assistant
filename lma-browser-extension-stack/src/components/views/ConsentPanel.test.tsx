/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import ConsentPanel from './ConsentPanel';
import { ConsentSettings, ConsentStep, emptyStep } from '../../lib/consent';

const NOTIFY: ConsentSettings = { event: 'CONSENT_SETTINGS', available: true, mode: 'notify', supported: true, noticeTemplate: 'Recorded.' };

/** The panel with its step held in state, as Capture.tsx holds it. */
function Harness({ settings = NOTIFY, capturing = false, onPostInChat = async () => true, onObjection = jest.fn(), onStep = jest.fn() }: {
  settings?: ConsentSettings, capturing?: boolean, onPostInChat?: () => Promise<boolean>, onObjection?: (name: string) => void, onStep?: (s: ConsentStep) => void,
}) {
  const [step, setStep] = React.useState<ConsentStep>(emptyStep());
  return (
    <ConsentPanel
      settings={settings}
      noticeText="Ana is recording this meeting."
      step={step}
      onStepChange={(next) => { setStep(next); onStep(next); }}
      onPostInChat={onPostInChat}
      capturing={capturing}
      status={null}
      onObjection={onObjection}
    />
  );
}

describe('ConsentPanel', () => {
  test('shows the notice with Copy and Post in meeting chat', () => {
    render(<Harness />);
    expect(screen.getByTestId('consent-notice').textContent).toBe('Ana is recording this meeting.');
    expect(screen.getByRole('button', { name: /Copy/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Post in meeting chat' })).toBeTruthy();
  });

  test('counts the chat post only once the user says it appeared, and then chooses the chat as the method', async () => {
    const onPostInChat = jest.fn(async () => true);
    const onStep = jest.fn();
    render(<Harness onPostInChat={onPostInChat} onStep={onStep} />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Post in meeting chat' })); });
    expect(onPostInChat).toHaveBeenCalled();
    expect(screen.getByText('Did the notice appear in the meeting chat?')).toBeTruthy();
    expect(onStep).toHaveBeenLastCalledWith(expect.objectContaining({ chatPost: 'sent' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, it appeared' }));
    expect(onStep).toHaveBeenLastCalledWith(expect.objectContaining({ chatPost: 'confirmed', method: 'chat' }));
    expect(screen.getByText('Posted in the meeting chat.')).toBeTruthy();
  });

  test('says so when the notice could not be posted', async () => {
    render(<Harness onPostInChat={async () => false} />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Post in meeting chat' })); });
    expect(screen.getByText(/The notice was not posted/)).toBeTruthy();
  });

  test('asks how everyone was told once the user confirms', () => {
    const onStep = jest.fn();
    render(<Harness settings={{ ...NOTIFY, mode: 'opt_in' }} onStep={onStep} />);
    fireEvent.click(screen.getByRole('checkbox', { name: /Everyone in the meeting agreed/ }));
    fireEvent.click(screen.getByRole('radio', { name: /Out loud/ }));
    expect(onStep).toHaveBeenLastCalledWith(expect.objectContaining({ confirmed: true, method: 'verbal' }));
  });

  test('offers Participant objected while capturing, with the name given', () => {
    const onObjection = jest.fn();
    render(<Harness capturing onObjection={onObjection} />);
    fireEvent.change(screen.getByLabelText('Name of the participant who objected'), { target: { value: ' Bo Chen ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Participant objected' }));
    expect(onObjection).toHaveBeenCalledWith('Bo Chen');
  });
});
