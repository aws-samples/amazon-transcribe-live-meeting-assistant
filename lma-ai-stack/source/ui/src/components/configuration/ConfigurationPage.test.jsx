/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The configuration editor against a mocked GraphQL client and the section
 * schemas exactly as the configuration layer ships them.
 *
 * What is asserted is what would be silent if wrong: a save sends only the
 * sections that changed, as deltas over the defaults, against the version that
 * was loaded; a value the server would refuse is caught before the save; and
 * a save refused because someone else saved first keeps the admin's changes
 * when they ask for that, over what the other person saved.
 */
import React from 'react';
import { readFileSync, readdirSync } from 'fs';
import { join } from 'path';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsContext } from '../../contexts/settings';
import { CONFIGURATION_PATH, MOVED_CONFIGURATION_PAGES } from '../../routes/constants';
import movedConfigurationRoutes from '../../routes/moved-configuration-routes';
import { ConfigurationContent } from './ConfigurationPage';

// The page renders every section's form; under a full parallel run that can take
// longer than the defaults allow, both for an element to appear and for a test.
configure({ asyncUtilTimeout: 10000 });
vi.setConfig({ testTimeout: 30000 });

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));

const SCHEMA_DIR = join(import.meta.dirname, '../../../../lambda_layers/configuration_layer/lma_config/schemas');
const TIERS = {
  summary_prompt_templates: 'standard',
  chat_buttons: 'immediate',
  nova_sonic_config: 'immediate',
  asr_engine: 'short',
  call_pricing: 'standard',
  language_models: 'standard',
  transcription: 'immediate',
  recording: 'immediate',
  meeting_assist: 'standard',
  meeting_processing: 'standard',
  meeting_consent: 'immediate',
  virtual_participant: 'immediate',
  retention: 'standard',
};
const SECTIONS = readdirSync(SCHEMA_DIR)
  .filter((name) => name.endsWith('.json'))
  .map((file) => {
    const name = file.replace('.json', '');
    return { Name: name, CacheTier: TIERS[name], Schema: JSON.parse(readFileSync(join(SCHEMA_DIR, file), 'utf8')) };
  });

const DEFAULTS = {
  summary_prompt_templates: { '1#SUMMARY': 'Summarize {transcript}', '2#DETAILS': 'Details of {transcript}' },
  chat_buttons: { '1#SUMMARIZE': 'Summarize this meeting', '2#ACTIONS': 'What were the action items?' },
  nova_sonic_config: {
    systemPrompt: 'You are Alex.',
    promptMode: 'base',
    modelId: 'amazon.nova-2-sonic-v1:0',
    voiceId: 'tiffany',
    endpointingSensitivity: 'MEDIUM',
    meetingMode: 'normal',
    translatorLanguageA: 'English',
    translatorLanguageB: 'Spanish',
    groupMeetingMode: false,
  },
  asr_engine: {
    streamingEngineMicrovm: false,
    virtualParticipantEngineMicrovm: false,
    diarizeVirtualParticipant: false,
  },
  call_pricing: {
    currency: 'USD',
    baseRegion: 'us-east-1',
    regions: { default: { 'transcribe/streaming': { seconds: 0.0001667 } } },
    pricedRegions: ['us-east-1'],
  },
  // What the seeder writes from the stack parameters that are these defaults.
  language_models: {
    summaryModelId: 'global.anthropic.claude-sonnet-5-5',
    meetingAssistModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
    knowledgeBaseModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
    keyframeTextModelId: 'amazon.nova-lite-v1:0',
  },
  transcription: {
    languageCode: 'en-US',
    languageOptions: 'en-US, es-US',
    preferredLanguage: 'None',
    contentRedactionEnabled: false,
    piiEntityTypes: 'NAME,EMAIL',
    customVocabularyName: '',
    customLanguageModelName: '',
    showSpeakerLabel: false,
  },
  recording: { recordAudio: true },
  meeting_assist: { wakePhraseRegex: '(OK|Okay)[.,! ]*[Aa]ssistant' },
  meeting_processing: {
    transcriptHookNonPartialOnly: true,
    inactiveMeetingLookbackDays: 2,
    inactiveMeetingTimeoutMinutes: 240,
  },
  retention: { meetingRecordExpirationInDays: 90, transcriptionExpirationInDays: 30 },
  meeting_consent: {
    mode: 'off',
    consentRule: 'each_participant',
    notifyNoticeText:
      '{LMA_USER} is using Live Meeting Assistant to record and transcribe this meeting ({meeting}). If you do not agree, reply "{objection_phrase}" in the chat or say so, and recording and transcription will stop.',
    optInNoticeText:
      '{LMA_USER} would like to record and transcribe this meeting ({meeting}) with Live Meeting Assistant. Nothing is recorded or transcribed until everyone has agreed. Reply "{consent_phrase}" to agree, or "{objection_phrase}" to decline.',
    consentPhrases: 'I consent, I agree, I accept',
    objectionPhrases:
      "I object, I do not consent, I don't consent, I do not agree, I don't agree, do not record, don't record, stop recording",
    allowPerMeetingOverride: true,
  },
  virtual_participant: {
    voiceAssistantProvider: 'none',
    voiceAssistantActivationMode: 'wake_phrase',
    voiceAssistantWakePhrases: 'hey alex,ok alex',
    voiceAssistantActivationDuration: 30,
    elevenLabsAgentId: '',
    simliFaceId: '',
    attendeePollMs: 20000,
    pollsBeforeEnd: 3,
    pollsBeforeEndMissing: 15,
  },
};

const PRESETS = [
  {
    Id: 'translator-voice-assistant',
    Title: 'Translator voice assistant',
    Description: 'Translator mode.',
    Sections: { nova_sonic_config: { mode: 'merge', values: { meetingMode: 'translator' } } },
  },
];

const answer = ({ version = 4, custom = {}, sections = SECTIONS } = {}) => ({
  data: {
    getConfiguration: {
      Profile: 'custom',
      Exists: true,
      Version: version,
      UpdatedBy: 'someone@example.com',
      UpdatedAt: '2026-10-31T10:00:00Z',
      PublishedRevision: 9,
      Sections: JSON.stringify(sections),
      Default: JSON.stringify(DEFAULTS),
      Custom: JSON.stringify(custom),
      Presets: JSON.stringify(PRESETS),
    },
  },
});

const saved = (revision = 10, version = 5) => ({
  data: {
    updateConfiguration: { Success: true, Profile: 'custom', Version: version, Revision: revision, Unchanged: false },
  },
});

const operationOf = (call) => call[0].query.match(/(query|mutation) (\w+)/)[2];
const callsOf = (name) => graphqlMock.mock.calls.filter((call) => operationOf(call) === name);

const renderEditor = ({ settings = {}, route = '/configuration/settings' } = {}) =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <SettingsContext.Provider value={{ settings }}>
        <ConfigurationContent />
      </SettingsContext.Provider>
    </MemoryRouter>,
  );

// A section's container, found from its heading.
const section = (heading) =>
  (typeof heading === 'string' ? screen.getByText(heading) : heading).closest('[id^="configuration-section-"]');

const openSection = (heading) => fireEvent.click(typeof heading === 'string' ? screen.getByText(heading) : heading);

const pageButton = (name) => screen.getAllByRole('button', { name })[0];

describe('ConfigurationContent', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  it('renders one section per schema, with engine choices only where the on-demand engine is deployed', async () => {
    graphqlMock.mockResolvedValue(answer());
    const { unmount } = renderEditor();
    await screen.findByText('Transcript Summary');
    expect(screen.getByText('Meeting Assist Chat Buttons')).toBeTruthy();
    expect(screen.getByText('Nova Sonic Voice Assistant')).toBeTruthy();
    expect(screen.getByText('Meeting Cost Prices')).toBeTruthy();
    // Voice separation works on Amazon Transcribe, so a deployment without the
    // on-demand engine still has the section, with only that switch in it.
    openSection('Transcription Engine');
    const transcribeOnly = section('Transcription Engine');
    expect(within(transcribeOnly).getByText('Virtual Participant voice separation')).toBeTruthy();
    expect(within(transcribeOnly).queryByText('Virtual Participants')).toBeNull();
    expect(within(transcribeOnly).queryByText('Stream Audio, Chrome extension and Desktop Capture')).toBeNull();
    expect(within(transcribeOnly).getByText('Every meeting is transcribed by Amazon Transcribe')).toBeTruthy();
    expect(within(transcribeOnly).queryByText(/Experimental/)).toBeNull();
    unmount();

    renderEditor({ settings: { AsrEngineAvailable: 'true' } });
    openSection(await screen.findByText('Transcription Engine'));
    const withEngine = section('Transcription Engine');
    expect(within(withEngine).getByText('Virtual Participants')).toBeTruthy();
    expect(within(withEngine).getByText('Stream Audio, Chrome extension and Desktop Capture')).toBeTruthy();
    expect(within(withEngine).getByText('Virtual Participant voice separation')).toBeTruthy();
    expect(within(withEngine).getByText(/Experimental/)).toBeTruthy();
    expect(within(withEngine).queryByText('Every meeting is transcribed by Amazon Transcribe')).toBeNull();
  });

  it('says the meeting modes are ignored while the saved activation mode is not always listening', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    const { unmount } = renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    const notice = within(section('Nova Sonic Voice Assistant')).getByText(
      /Virtual Participant section sets the activation mode/,
    );
    expect(notice.textContent).toMatch(/sets the activation mode to Wake phrase, so those modes are ignored/);

    expect(screen.getAllByText('Voice assistant activation mode').length).toBeGreaterThan(0);
    unmount();

    graphqlMock.mockResolvedValueOnce(
      answer({ custom: { virtual_participant: { voiceAssistantActivationMode: 'always_active' } } }),
    );
    const { unmount: unmountCustom } = renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    expect(
      within(section('Nova Sonic Voice Assistant')).queryByText(/Virtual Participant section sets the activation mode/),
    ).toBeNull();
    unmountCustom();

    // A deployment whose stack parameter is always_active, with nothing saved
    // on the page: the default is what is in force, so there is no notice.
    const alwaysByDefault = JSON.parse(answer().data.getConfiguration.Default);
    alwaysByDefault.virtual_participant.voiceAssistantActivationMode = 'always_active';
    const response = answer();
    response.data.getConfiguration.Default = JSON.stringify(alwaysByDefault);
    graphqlMock.mockResolvedValueOnce(response);
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    expect(
      within(section('Nova Sonic Voice Assistant')).queryByText(/Virtual Participant section sets the activation mode/),
    ).toBeNull();
  });

  it('refuses a retention longer than the limit the deployment sets, and saves a shorter one', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    openSection(await screen.findByText('Retention'));
    const retention = section('Retention');
    expect(within(retention).getAllByText(/At most 90, the limit this deployment sets/)).toHaveLength(1);

    const days = within(retention).getByLabelText('Days to keep meeting records');
    fireEvent.change(days, { target: { value: '120' } });
    // Beside the field, and in the section's list of what stops the save.
    expect(
      await within(retention).findAllByText(/must be at most 90, the limit this deployment sets; got 120/),
    ).toHaveLength(2);
    expect(pageButton('Save changes').disabled).toBe(true);

    fireEvent.change(days, { target: { value: '7.5' } });
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      retention: { meetingRecordExpirationInDays: 7.5 },
    });
  });

  it('saves Virtual Participant voice separation on a deployment without the on-demand engine', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    openSection(await screen.findByText('Transcription Engine'));
    const engine = section('Transcription Engine');
    fireEvent.click(within(engine).getByRole('checkbox'));
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    // Only the switch changed; the hidden engine choices keep following the defaults.
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      asr_engine: { diarizeVirtualParticipant: true },
    });
  });

  it('marks a changed value, and saves only that section, as a delta, against the loaded version', async () => {
    graphqlMock.mockResolvedValueOnce(answer({ custom: { nova_sonic_config: { promptMode: 'inject' } } }));
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    const nova = section('Nova Sonic Voice Assistant');

    // The stored change is marked as loaded.
    expect(within(nova).getAllByText('Changed')).toHaveLength(1);
    expect(pageButton('Save changes').disabled).toBe(true);

    fireEvent.change(within(nova).getByLabelText('Voice ID'), { target: { value: 'matthew' } });
    expect(within(nova).getAllByText('Changed')).toHaveLength(2);

    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    expect(within(dialog).getByText(/Nova Sonic Voice Assistant: 1 setting\./)).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Comment'), { target: { value: 'deeper voice' } });
    fireEvent.change(within(dialog).getByLabelText('Label'), { target: { value: 'before launch' } });

    graphqlMock
      .mockResolvedValueOnce(saved(10))
      .mockResolvedValueOnce({ data: { labelConfigurationRevision: { Success: true, Revision: 10 } } })
      .mockResolvedValueOnce(
        answer({ version: 5, custom: { nova_sonic_config: { promptMode: 'inject', voiceId: 'matthew' } } }),
      );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await screen.findByText(/Saved as revision 10\./);
    const [update] = callsOf('UpdateConfiguration');
    const { input } = update[0].variables;
    expect(input.ExpectedVersion).toBe(4);
    expect(input.Comment).toBe('deeper voice');
    // Only the section that changed, and only what differs from the defaults.
    expect(JSON.parse(input.Sections)).toEqual({ nova_sonic_config: { promptMode: 'inject', voiceId: 'matthew' } });
    const [labelCall] = callsOf('LabelConfigurationRevision');
    expect(labelCall[0].variables.input).toEqual({ Profile: 'custom', Revision: 10, Label: 'before launch' });
  });

  it('puts back the shipped value of one setting', async () => {
    graphqlMock.mockResolvedValueOnce(answer({ custom: { nova_sonic_config: { voiceId: 'amy' } } }));
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    const nova = section('Nova Sonic Voice Assistant');
    fireEvent.click(within(nova).getByText('Reset to default'));
    expect(within(nova).queryByText('Changed')).toBeNull();
    // A change from what was loaded: saving it stores an empty section.
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      nova_sonic_config: {},
    });
  });

  it('shows the deployed models as the defaults, marks a changed one, and resets it to the deployed one', async () => {
    graphqlMock.mockResolvedValueOnce(
      answer({ custom: { language_models: { summaryModelId: 'global.anthropic.claude-haiku-4-5-20251001-v1:0' } } }),
    );
    renderEditor();
    openSection(await screen.findByText('Amazon Bedrock Models'));
    const models = section('Amazon Bedrock Models');
    expect(within(models).getAllByText('Summaries and meeting insights').length).toBeGreaterThan(0);
    expect(within(models).getAllByText('Key frame text').length).toBeGreaterThan(0);
    expect(within(models).getAllByText('Changed')).toHaveLength(1);
    fireEvent.click(within(models).getByText('Reset to default'));
    expect(within(models).queryByText('Changed')).toBeNull();
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    // Reset stores an empty section, so the stack parameter's value applies again.
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      language_models: {},
    });
  });

  it('shows the redacted entity types only while redaction is on', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    openSection(await screen.findByText('Amazon Transcribe'));
    const transcribe = section('Amazon Transcribe');
    expect(within(transcribe).getAllByText('Custom vocabulary').length).toBeGreaterThan(0);
    expect(within(transcribe).queryByText('Entity types to redact')).toBeNull();
    fireEvent.click(within(transcribe).getByRole('checkbox', { name: /Redact personal information/ }));
    expect(within(transcribe).getAllByText('Entity types to redact').length).toBeGreaterThan(0);
  });

  it('refuses to save a value the server would refuse, and says where it is', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor({ route: '/configuration/settings?section=call_pricing' });
    const prices = section(await screen.findByText('Meeting Cost Prices'));
    // The deep link opened this section: its nested entries are listed.
    fireEvent.click(within(prices).getByText('default'));
    fireEvent.click(within(prices).getByText('transcribe/streaming'));
    fireEvent.change(within(prices).getByLabelText('seconds'), { target: { value: '-1' } });

    expect(await within(prices).findAllByText(/must be at least 0; got -1/)).not.toHaveLength(0);
    expect(screen.getByText(/1 setting needs correcting/)).toBeTruthy();
    expect(pageButton('Save changes').disabled).toBe(true);
  });

  it('turns a shipped summary section off, and back on, without touching the others', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    const summary = section(await screen.findByText('Transcript Summary'));
    const details = within(summary).getByText('DETAILS').closest('.lma-config-field');
    fireEvent.click(within(details).getByRole('button', { name: 'Turn off' }));
    expect(within(summary).getByText('Turned off')).toBeTruthy();

    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      summary_prompt_templates: { '2#DETAILS': 'NONE' },
    });
  });

  it('brings a turned-off summary section back with Turn on', async () => {
    graphqlMock.mockResolvedValueOnce(answer({ custom: { summary_prompt_templates: { '2#DETAILS': 'NONE' } } }));
    renderEditor();
    const summary = section(await screen.findByText('Transcript Summary'));
    fireEvent.click(within(summary).getByRole('button', { name: 'Turn on' }));
    expect(within(summary).queryByText('Turned off')).toBeNull();
  });

  it('on a conflict, keeps the changes made here over what the other save stored', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    const nova = section('Nova Sonic Voice Assistant');
    fireEvent.change(within(nova).getByLabelText('Voice ID'), { target: { value: 'matthew' } });

    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockRejectedValueOnce({ errors: [{ message: 'Conflict: the configuration profile was saved' }] });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText('Someone else saved the configuration after this page loaded it');

    // The other save changed a different Nova Sonic setting.
    graphqlMock.mockResolvedValueOnce(answer({ version: 6, custom: { nova_sonic_config: { promptMode: 'replace' } } }));
    fireEvent.click(screen.getByRole('button', { name: 'Reload and keep my changes' }));
    await screen.findByText(/Your changes are applied over the configuration as it is now saved/);

    fireEvent.click(pageButton('Save changes'));
    const again = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved(11, 7)).mockResolvedValueOnce(answer({ version: 7 }));
    fireEvent.click(within(again).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision 11/);
    const { input } = callsOf('UpdateConfiguration')[1][0].variables;
    expect(input.ExpectedVersion).toBe(6);
    expect(JSON.parse(input.Sections)).toEqual({ nova_sonic_config: { promptMode: 'replace', voiceId: 'matthew' } });
  });

  it('shows a refusal from the server with the problems it names', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    fireEvent.change(within(section('Nova Sonic Voice Assistant')).getByLabelText('Voice ID'), {
      target: { value: 'matthew' },
    });
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockRejectedValueOnce({
      errors: [{ message: 'Invalid: the configuration was not saved: 1 problem: nova_sonic_config › voiceId: bad' }],
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByText(/The configuration was refused: .*voiceId: bad/)).toBeTruthy());
  });

  it('adds a price for a region the shipped table does not list, and refuses a malformed region name', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor({ route: '/configuration/settings?section=call_pricing' });
    const prices = section(await screen.findByText('Meeting Cost Prices'));
    const addRegion = (name) => {
      fireEvent.change(within(prices).getByLabelText('New region name'), { target: { value: name } });
      fireEvent.click(within(prices).getByRole('button', { name: 'Add the new region' }));
    };
    addRegion('Mars');
    expect(within(prices).getByText(/A region name must match/)).toBeTruthy();
    addRegion('eu-west-1');
    fireEvent.click(within(prices).getByText('eu-west-1'));
    const region = within(within(prices).getByText('eu-west-1').closest('.lma-config-field'));
    fireEvent.change(region.getByLabelText('New service name'), { target: { value: 'transcribe/streaming' } });
    fireEvent.click(region.getByRole('button', { name: 'Add the new service' }));
    fireEvent.click(region.getByText('transcribe/streaming'));
    fireEvent.change(region.getByLabelText('New unit name'), { target: { value: 'seconds' } });
    fireEvent.click(region.getByRole('button', { name: 'Add the new unit' }));
    fireEvent.change(region.getByLabelText('seconds'), { target: { value: '0.0002' } });

    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      call_pricing: { regions: { 'eu-west-1': { 'transcribe/streaming': { seconds: 0.0002 } } } },
    });
  });

  it('says why a section a link asked for is not shown', async () => {
    // No shipped section is withheld today, so the test withholds one.
    const withheld = SECTIONS.map((entry) =>
      entry.Name === 'call_pricing'
        ? { ...entry, Schema: { ...entry.Schema, availableWhen: { setting: 'PricingAvailable', value: 'true' } } }
        : entry,
    );
    graphqlMock.mockResolvedValueOnce(answer({ sections: withheld }));
    renderEditor({ route: '/configuration/settings?section=call_pricing' });
    expect(await screen.findByText('Meeting Cost Prices is not available in this deployment')).toBeTruthy();
    expect(screen.getByText(/depends on a part of LMA this deployment does not include/)).toBeTruthy();
  });

  it('opens the Transcription Engine section from a link on a deployment without the on-demand engine', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor({ route: '/configuration/settings?section=asr_engine' });
    expect(await screen.findByText('Virtual Participant voice separation')).toBeTruthy();
    expect(screen.queryByText(/is not available in this deployment/)).toBeNull();
  });

  it('reorders the chat buttons, renumbering them from one, and stores the list whole', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    openSection(await screen.findByText('Meeting Assist Chat Buttons'));
    const buttons = section('Meeting Assist Chat Buttons');
    fireEvent.click(within(buttons).getByRole('button', { name: 'Move ACTIONS up' }));
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    // A replace list is stored whole.
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      chat_buttons: { '1#ACTIONS': 'What were the action items?', '2#SUMMARIZE': 'Summarize this meeting' },
    });
  });
});

describe('import, export and presets', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  const menuItem = async (text) => {
    fireEvent.click(screen.getByRole('button', { name: 'Import, export and presets' }));
    fireEvent.click(await screen.findByText(text));
  };

  const saveAndRead = async () => {
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    const comment = within(dialog).getByLabelText('Comment').value;
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    const { input } = callsOf('UpdateConfiguration')[0][0].variables;
    return { comment, sections: JSON.parse(input.Sections), input };
  };

  it('applies a preset to the page only, and saves it with a comment naming it', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    await screen.findByText('Nova Sonic Voice Assistant');
    await menuItem('Apply a preset');
    fireEvent.click(await screen.findByText('Translator voice assistant'));
    const dialog = await screen.findByRole('dialog', { name: /Apply "Translator voice assistant"/ });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply to the page' }));
    expect(await screen.findByText(/Nothing is saved yet/)).toBeTruthy();
    expect(callsOf('UpdateConfiguration')).toHaveLength(0);

    const { comment, sections } = await saveAndRead();
    expect(comment).toBe('Applied the preset "Translator voice assistant"');
    expect(sections).toEqual({ nova_sonic_config: { meetingMode: 'translator' } });
  });

  it('imports a file as unsaved changes, and saves exactly what it held', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    await screen.findByText('Nova Sonic Voice Assistant');
    const file = new File(
      [
        JSON.stringify({
          format: 'lma-configuration-profile',
          formatVersion: 1,
          sections: { nova_sonic_config: { voiceId: 'amy' }, not_here: {} },
        }),
      ],
      'staging.json',
      { type: 'application/json' },
    );
    fireEvent.change(screen.getByTestId('configuration-import'), { target: { files: [file] } });
    expect(await screen.findByText(/Loaded Nova Sonic Voice Assistant from staging.json/)).toBeTruthy();
    expect(screen.getByText(/no such section: not_here/)).toBeTruthy();

    const { comment, sections } = await saveAndRead();
    expect(comment).toBe('Imported from staging.json');
    expect(sections).toEqual({ nova_sonic_config: { voiceId: 'amy' } });
  });

  it('says when an import replaced unsaved edits', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    fireEvent.change(within(section('Nova Sonic Voice Assistant')).getByLabelText('Voice ID'), {
      target: { value: 'matthew' },
    });
    const file = new File(
      [JSON.stringify({ format: 'lma-configuration-profile', formatVersion: 1, sections: { nova_sonic_config: {} } })],
      'clean.json',
      { type: 'application/json' },
    );
    fireEvent.change(screen.getByTestId('configuration-import'), { target: { files: [file] } });
    expect(await screen.findByText(/It replaced unsaved edits you had made in those sections/)).toBeTruthy();
  });

  it('refuses a file that is not an export, and changes nothing', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    await screen.findByText('Nova Sonic Voice Assistant');
    const file = new File(['{"hello": 1}'], 'other.json', { type: 'application/json' });
    fireEvent.change(screen.getByTestId('configuration-import'), { target: { files: [file] } });
    expect(
      await screen.findByText(/Could not import other.json: The file is not a configuration exported/),
    ).toBeTruthy();
    expect(pageButton('Save changes').disabled).toBe(true);
  });

  it('exports the configuration as saved, as a download', async () => {
    const created = [];
    const createObjectURL = vi.fn((blob) => {
      created.push(blob);
      return 'blob:configuration';
    });
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    graphqlMock.mockResolvedValueOnce(answer({ custom: { nova_sonic_config: { voiceId: 'amy' } } }));
    renderEditor({ settings: { StackName: 'LMA-test' } });
    await screen.findByText('Nova Sonic Voice Assistant');
    await menuItem('Export to a file');

    expect(click).toHaveBeenCalled();
    const exported = JSON.parse(await created[0].text());
    expect(exported.format).toBe('lma-configuration-profile');
    expect(exported.stackName).toBe('LMA-test');
    expect(exported.revision).toBe(9);
    expect(exported.sections.nova_sonic_config).toEqual({ voiceId: 'amy' });
    expect(exported.sections.call_pricing).toEqual({});
    click.mockRestore();
    vi.unstubAllGlobals();
  });
});

describe('the pages that each edited one section', () => {
  const Where = () => {
    const location = useLocation();
    return <span>{`${location.pathname}${location.search}`}</span>;
  };

  it.each(Object.entries(MOVED_CONFIGURATION_PAGES))(
    'send %s to its section of the Configuration page',
    (path, name) => {
      expect(SECTIONS.map((entry) => entry.Name)).toContain(name);
      // The routes AuthRoutes renders.
      render(
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            {movedConfigurationRoutes()}
            <Route path={`${CONFIGURATION_PATH}/*`} element={<Where />} />
          </Routes>
        </MemoryRouter>,
      );
      expect(screen.getByText(`${CONFIGURATION_PATH}?section=${name}`)).toBeTruthy();
    },
  );
});
