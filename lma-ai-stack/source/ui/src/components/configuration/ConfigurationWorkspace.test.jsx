/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The View / Edit Configuration page -- the profile list and the editor below
 * it -- against a mocked GraphQL client and the section schemas exactly as the
 * configuration layer ships them.
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
import { CONFIGURATION_HISTORY_PATH, CONFIGURATION_PATH, MOVED_CONFIGURATION_PAGES } from '../../routes/constants';
import movedConfigurationRoutes from '../../routes/moved-configuration-routes';
import { ConfigurationWorkspaceContent } from './ConfigurationWorkspace';

// The page renders every section's form; under a full parallel run that can take
// longer than the defaults allow, both for an element to appear and for a test.
configure({ asyncUtilTimeout: 10000 });
vi.setConfig({ testTimeout: 30000 });

const { graphqlMock, profilesMock } = vi.hoisted(() => ({ graphqlMock: vi.fn(), profilesMock: vi.fn() }));
// The profile list is answered on its own, so each test's queue of answers is
// the configuration reads and saves it is about.
vi.mock('aws-amplify/api', () => ({
  generateClient: () => ({
    graphql: (request) =>
      /query ListConfigurationProfiles/.test(request.query) ? profilesMock(request) : graphqlMock(request),
  }),
}));

const PROFILES = {
  data: {
    listConfigurationProfiles: {
      Profiles: [
        { Name: 'default', Managed: true, Active: false },
        { Name: 'custom', Managed: false, Active: true },
      ],
    },
  },
};

const SCHEMA_DIR = join(import.meta.dirname, '../../../../lambda_layers/configuration_layer/lma_config/schemas');
const CONSENT_DEFAULTS = JSON.parse(
  readFileSync(
    join(import.meta.dirname, '../../../../lambda_functions/configuration_seeder/defaults/MeetingConsent.json'),
    'utf8',
  ),
);
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
  meeting_agenda: 'standard',
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
  // The shipped defaults, read from the file the seeder ships them in.
  meeting_consent: CONSENT_DEFAULTS,
  meeting_agenda: { autoDeriveAfterSeconds: 300 },
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
    captureMeetingChat: true,
  },
};

// Served out of order, to show the editor lists them by Order.
const PRESETS = [
  {
    Id: 'internal-meetings-notify',
    Title: 'Internal meetings – notify participants',
    Description: 'Participants are notified.',
    Order: 1,
    Sections: { meeting_consent: { mode: 'merge', values: { mode: 'notify' } } },
  },
  {
    Id: 'standard',
    Title: 'Standard',
    Description: 'The shipped defaults.',
    Order: 0,
    Sections: { meeting_consent: { mode: 'merge', values: { mode: 'off' } } },
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

// Downloads are made with URL.createObjectURL, which jsdom lacks. Only the two
// static methods are replaced: the router constructs URLs as it navigates.
const stubDownloads = () => {
  const created = [];
  const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  URL.createObjectURL = vi.fn((blob) => {
    created.push(blob);
    return 'blob:download';
  });
  URL.revokeObjectURL = vi.fn();
  return {
    created,
    restore: () => {
      URL.createObjectURL = original.create;
      URL.revokeObjectURL = original.revoke;
    },
  };
};

const operationOf = (call) => call[0].query.match(/(query|mutation) (\w+)/)[2];
const callsOf = (name) => graphqlMock.mock.calls.filter((call) => operationOf(call) === name);

const renderEditor = ({ settings = {}, route = '/configuration/settings' } = {}) =>
  render(
    <MemoryRouter initialEntries={[route]}>
      <SettingsContext.Provider value={{ settings }}>
        <ConfigurationWorkspaceContent />
      </SettingsContext.Provider>
    </MemoryRouter>,
  );

// A section's container, found from its heading.
const section = (heading) =>
  (typeof heading === 'string' ? screen.getByText(heading) : heading).closest('[id^="configuration-section-"]');

const openSection = (heading) => fireEvent.click(typeof heading === 'string' ? screen.getByText(heading) : heading);

const pageButton = (name) => screen.getAllByRole('button', { name })[0];

// A Cloudscape button with a reason for being disabled is aria-disabled, so
// its reason can still be focused and read, rather than disabled.
const isDisabled = (element) => element.disabled || element.getAttribute('aria-disabled') === 'true';

describe('the editor', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
    profilesMock.mockReset();
    profilesMock.mockResolvedValue(PROFILES);
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

  it('offers the meeting chat switch in the Virtual Participant section, on by default, and saves it off', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    openSection(await screen.findByText('Virtual Participant'));
    const vp = section('Virtual Participant');
    const toggle = within(vp).getByRole('checkbox', { name: /Add the meeting chat to the transcript/ });
    expect(toggle.checked).toBe(true);
    fireEvent.click(toggle);
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      virtual_participant: { captureMeetingChat: false },
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

describe('configuration profiles', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
    profilesMock.mockReset();
  });

  const managedAnswer = () => {
    const response = answer();
    Object.assign(response.data.getConfiguration, { Profile: 'default', Managed: true, Active: true });
    return response;
  };

  it('opens the active profile, and shows the default read-only with Copy to a new profile', async () => {
    profilesMock.mockResolvedValue({
      data: {
        listConfigurationProfiles: { Profiles: [{ Name: 'default', Managed: true, Active: true }] },
      },
    });
    graphqlMock.mockResolvedValue(managedAnswer());
    renderEditor();
    expect(await screen.findByText('The deployment default is read-only')).toBeTruthy();
    expect(callsOf('GetConfiguration')[0][0].variables).toEqual({ Profile: 'default' });
    expect(screen.getAllByRole('button', { name: 'Copy to a new profile' }).length).toBeGreaterThan(0);
  });

  it('copies the profile shown to a new Custom profile, and opens the copy', async () => {
    profilesMock.mockResolvedValue({
      data: {
        listConfigurationProfiles: { Profiles: [{ Name: 'default', Managed: true, Active: true }] },
      },
    });
    graphqlMock.mockImplementation(async (request) => {
      if (/mutation CreateConfigurationProfile/.test(request.query)) {
        return { data: { createConfigurationProfile: { Success: true, Profile: 'custom', Version: 1 } } };
      }
      return managedAnswer();
    });
    renderEditor();
    await screen.findByText('The deployment default is read-only');
    fireEvent.click(screen.getAllByRole('button', { name: 'Copy to a new profile' })[0]);
    fireEvent.click(await screen.findByRole('button', { name: 'Create' }));
    await waitFor(() => expect(callsOf('CreateConfigurationProfile')).toHaveLength(1));
    expect(callsOf('CreateConfigurationProfile')[0][0].variables.input).toEqual({
      Name: 'custom',
      CopyFrom: 'default',
      Description: null,
    });
    await waitFor(() => expect(callsOf('GetConfiguration').at(-1)[0].variables).toEqual({ Profile: 'custom' }));
  });

  it('saves into the profile it shows', async () => {
    profilesMock.mockResolvedValue({
      data: {
        listConfigurationProfiles: {
          Profiles: [
            { Name: 'default', Managed: true, Active: false },
            { Name: 'trial', Managed: false, Active: true },
          ],
        },
      },
    });
    const response = answer();
    Object.assign(response.data.getConfiguration, { Profile: 'trial', Active: true });
    graphqlMock.mockResolvedValue(response);
    renderEditor();
    await screen.findByText('Transcript Summary');
    expect(callsOf('GetConfiguration')[0][0].variables).toEqual({ Profile: 'trial' });
    expect(screen.queryByText('The deployment default is read-only')).toBeNull();
  });
});

describe('import, export and presets', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
    profilesMock.mockReset();
    profilesMock.mockResolvedValue(PROFILES);
  });

  const menuItem = async (text) => {
    fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
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
    await menuItem('Apply a preset…');
    const dialog = await screen.findByRole('dialog', { name: 'Apply a preset' });
    expect(within(dialog).getByRole('button', { name: 'Apply to the page' }).disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole('radio', { name: /Internal meetings/ }));
    expect(within(dialog).getByText('Meeting Consent: Consent mode change, and the others stay')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply to the page' }));
    expect(await screen.findByText(/Nothing is saved yet/)).toBeTruthy();
    expect(callsOf('UpdateConfiguration')).toHaveLength(0);

    const { comment, sections } = await saveAndRead();
    expect(comment).toBe('Applied the preset "Internal meetings – notify participants"');
    expect(sections).toEqual({ meeting_consent: { mode: 'notify' } });
  });

  it('lists the presets as one group, in their order, each with its description', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    await screen.findByText('Nova Sonic Voice Assistant');
    await menuItem('Apply a preset…');
    const dialog = await screen.findByRole('dialog', { name: 'Apply a preset' });
    expect(within(dialog).getAllByRole('radiogroup')).toHaveLength(1);
    const radios = within(dialog).getAllByRole('radio');
    expect(radios.map((radio) => radio.value)).toEqual(['standard', 'internal-meetings-notify']);
    expect(within(dialog).getByText('The shipped defaults.')).toBeTruthy();
    expect(within(dialog).getByText('Participants are notified.')).toBeTruthy();
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
    const { created, restore } = stubDownloads();
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
    restore();
  });
});

describe('the JSON and YAML views, and comparing profiles', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
    profilesMock.mockReset();
    profilesMock.mockResolvedValue(PROFILES);
  });

  const textView = async (name) => {
    fireEvent.click(screen.getByRole('button', { name }));
    return screen.findByLabelText(`Configuration ${name}`);
  };

  it('shows every section as JSON, and saves an edit typed there as the form would', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    await screen.findByText('Transcript Summary');
    const editor = await textView('JSON');
    const shown = JSON.parse(editor.value);
    expect(shown.language_models).toEqual(DEFAULTS.language_models);
    expect(shown.meeting_processing.inactiveMeetingTimeoutMinutes).toBe(240);

    shown.meeting_processing.inactiveMeetingTimeoutMinutes = 60;
    fireEvent.change(editor, { target: { value: JSON.stringify(shown, null, 2) } });
    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    const { input } = callsOf('UpdateConfiguration')[0][0].variables;
    expect(Object.keys(JSON.parse(input.Sections))).toEqual(['meeting_processing']);
    expect(JSON.parse(input.Sections).meeting_processing.inactiveMeetingTimeoutMinutes).toBe(60);
    expect(input.ExpectedVersion).toBe(4);
  });

  it('shows YAML, and will not save or switch view while the text does not read', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    await screen.findByText('Transcript Summary');
    const editor = await textView('YAML');
    expect(editor.value).toContain('inactiveMeetingTimeoutMinutes: 240');

    fireEvent.change(editor, { target: { value: 'meeting_processing: [unclosed' } });
    expect(await screen.findByText(/not valid YAML/)).toBeTruthy();
    expect(pageButton('Save changes').disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Form' }).disabled).toBe(true);

    fireEvent.click(pageButton('Discard changes'));
    await waitFor(() => expect(screen.queryByText(/not valid YAML/)).toBeNull());
    expect(screen.getByLabelText('Configuration YAML').value).toContain('inactiveMeetingTimeoutMinutes: 240');
  });

  it('lists a value the schema refuses, by section and setting, under the text', async () => {
    graphqlMock.mockResolvedValueOnce(answer());
    renderEditor();
    await screen.findByText('Transcript Summary');
    const editor = await textView('JSON');
    const shown = JSON.parse(editor.value);
    shown.meeting_processing.inactiveMeetingTimeoutMinutes = 'soon';
    fireEvent.change(editor, { target: { value: JSON.stringify(shown) } });
    expect(await screen.findByText(/inactiveMeetingTimeoutMinutes: must be/)).toBeTruthy();
    expect(pageButton('Save changes').disabled).toBe(true);
  });
});

// Three profiles: the managed default, the active custom one, and an inactive trial.
const THREE_PROFILES = {
  data: {
    listConfigurationProfiles: {
      Profiles: [
        { Name: 'default', Managed: true, Active: false, Description: 'Deployment default' },
        { Name: 'custom', Managed: false, Active: true, Description: 'What meetings use' },
        { Name: 'trial', Managed: false, Active: false, Description: 'Trying a new model' },
      ],
    },
  },
};

// A profile's read, named as it is, holding `custom` over the defaults.
const answerFor = (name, custom = {}) => {
  const response = answer({ custom });
  Object.assign(response.data.getConfiguration, {
    Profile: name,
    Managed: name === 'default',
    Active: name === 'custom',
  });
  return response;
};

// The checkbox that selects a profile's row in the profile list.
const selectProfile = (name) => fireEvent.click(screen.getByRole('checkbox', { name: `Select profile ${name}` }));

describe('the profile list', () => {
  const STORED = {
    default: {},
    custom: { recording: { recordAudio: false } },
    trial: { recording: { recordAudio: false }, retention: { meetingRecordExpirationInDays: 30 } },
  };

  beforeEach(() => {
    graphqlMock.mockReset();
    profilesMock.mockReset();
    profilesMock.mockResolvedValue(THREE_PROFILES);
    graphqlMock.mockImplementation(async (request) => {
      if (/query GetConfiguration/.test(request.query)) {
        return answerFor(request.variables.Profile, STORED[request.variables.Profile]);
      }
      if (/mutation ActivateConfigurationProfile/.test(request.query)) {
        return { data: { activateConfigurationProfile: { Success: true, Profile: request.variables.input.Name } } };
      }
      if (/mutation DeleteConfigurationProfile/.test(request.query)) {
        return { data: { deleteConfigurationProfile: { Success: true, Profile: request.variables.input.Name } } };
      }
      if (/mutation CreateConfigurationProfile/.test(request.query)) {
        return { data: { createConfigurationProfile: { Success: true, Profile: request.variables.input.Name } } };
      }
      if (/query ListConfigurationRevisions/.test(request.query)) {
        return {
          data: { listConfigurationRevisions: { Profile: request.variables.Profile, Version: 1, Revisions: [] } },
        };
      }
      throw new Error(`unexpected request ${request.query}`);
    });
  });

  it('lists every profile, marks the active one and the one open, and opens the active one', async () => {
    renderEditor();
    await screen.findByText('Transcript Summary');
    expect(callsOf('GetConfiguration')[0][0].variables).toEqual({ Profile: 'custom' });
    const customRow = screen.getByRole('button', { name: 'Open custom' }).closest('tr');
    expect(within(customRow).getByText('Active')).toBeTruthy();
    expect(within(customRow).getByText('Open in the editor')).toBeTruthy();
    expect(within(customRow).getByText('Custom')).toBeTruthy();
    const defaultRow = screen.getByRole('button', { name: 'Open default' }).closest('tr');
    expect(within(defaultRow).getByText('Managed')).toBeTruthy();
    expect(within(defaultRow).queryByText('Open in the editor')).toBeNull();
    expect(within(defaultRow).getByText('Deployment default')).toBeTruthy();
  });

  it('opens a profile from its row, and names it in the address', async () => {
    const Where = () => <span data-testid="where">{useLocation().search}</span>;
    render(
      <MemoryRouter initialEntries={['/configuration/settings']}>
        <SettingsContext.Provider value={{ settings: {} }}>
          <ConfigurationWorkspaceContent />
          <Where />
        </SettingsContext.Provider>
      </MemoryRouter>,
    );
    await screen.findByText('Transcript Summary');
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('?profile=custom'));
    fireEvent.click(screen.getByRole('button', { name: 'Open trial' }));
    await waitFor(() => expect(callsOf('GetConfiguration').at(-1)[0].variables).toEqual({ Profile: 'trial' }));
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('?profile=trial'));
    const trialRow = screen.getByRole('button', { name: 'Open trial' }).closest('tr');
    expect(within(trialRow).getByText('Open in the editor')).toBeTruthy();
  });

  it('asks before opening another profile over unsaved changes, and stays when told to', async () => {
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    fireEvent.change(within(section('Nova Sonic Voice Assistant')).getByLabelText('Voice ID'), {
      target: { value: 'matthew' },
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    fireEvent.click(screen.getByRole('button', { name: 'Open trial' }));
    expect(confirm).toHaveBeenCalledWith('Open another profile? Your unsaved changes will be lost.');
    expect(callsOf('GetConfiguration').map((call) => call[0].variables.Profile)).toEqual(['custom']);
    expect(within(section('Nova Sonic Voice Assistant')).getByLabelText('Voice ID').value).toBe('matthew');

    confirm.mockReturnValueOnce(true);
    fireEvent.click(screen.getByRole('button', { name: 'Open trial' }));
    await waitFor(() => expect(callsOf('GetConfiguration').at(-1)[0].variables).toEqual({ Profile: 'trial' }));
    confirm.mockRestore();
  });

  it('offers each action only for a selection it can act on', async () => {
    renderEditor();
    await screen.findByText('Transcript Summary');
    const compare = () => screen.getByRole('button', { name: /^Compare profiles/ });
    expect(isDisabled(compare())).toBe(true);
    expect(isDisabled(pageButton('Activate'))).toBe(true);
    expect(isDisabled(pageButton('Delete'))).toBe(true);

    selectProfile('custom');
    // The active profile is already active, and cannot be deleted.
    expect(isDisabled(pageButton('Activate'))).toBe(true);
    expect(isDisabled(pageButton('Delete'))).toBe(true);
    expect(isDisabled(compare())).toBe(true);

    selectProfile('trial');
    expect(compare().textContent).toBe('Compare profiles (2)');
    expect(isDisabled(compare())).toBe(false);
    // Two selected: there is nothing single to activate.
    expect(isDisabled(pageButton('Activate'))).toBe(true);

    selectProfile('custom');
    expect(isDisabled(pageButton('Activate'))).toBe(false);
    expect(isDisabled(pageButton('Delete'))).toBe(false);

    selectProfile('default');
    // A managed profile is in the selection.
    expect(isDisabled(pageButton('Delete'))).toBe(true);
  });

  it('activates the profile selected', async () => {
    renderEditor();
    await screen.findByText('Transcript Summary');
    selectProfile('trial');
    fireEvent.click(pageButton('Activate'));
    expect(await screen.findByText(/The trial profile is now active/)).toBeTruthy();
    expect(callsOf('ActivateConfigurationProfile')[0][0].variables.input).toEqual({ Name: 'trial' });
  });

  it('deletes the profiles selected, after asking', async () => {
    profilesMock.mockResolvedValue({
      data: {
        listConfigurationProfiles: {
          Profiles: [...THREE_PROFILES.data.listConfigurationProfiles.Profiles, { Name: 'old', Managed: false }],
        },
      },
    });
    renderEditor();
    await screen.findByText('Transcript Summary');
    selectProfile('trial');
    selectProfile('old');
    fireEvent.click(pageButton('Delete'));
    const dialog = await screen.findByRole('dialog', { name: 'Delete 2 profiles?' });
    expect(callsOf('DeleteConfigurationProfile')).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));
    // In the order the list shows them.
    expect(await screen.findByText(/Deleted old, trial\. Their revisions stay in the history/)).toBeTruthy();
    expect(callsOf('DeleteConfigurationProfile').map((call) => call[0].variables.input.Name)).toEqual(['old', 'trial']);
  });

  it('compares three profiles as saved, a column each, and exports the comparison as CSV', async () => {
    const { created, restore } = stubDownloads();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    renderEditor();
    await screen.findByText('Transcript Summary');
    selectProfile('default');
    selectProfile('custom');
    selectProfile('trial');
    fireEvent.click(screen.getByRole('button', { name: 'Compare profiles (3)' }));
    const dialog = await screen.findByRole('dialog', { name: 'Compare default, custom, trial' });
    expect(await within(dialog).findByText('Settings that differ')).toBeTruthy();
    await waitFor(() =>
      expect(new Set(callsOf('GetConfiguration').map((call) => call[0].variables.Profile))).toEqual(
        new Set(['custom', 'default', 'trial']),
      ),
    );
    const headers = within(dialog)
      .getAllByRole('columnheader')
      .map((cell) => cell.textContent);
    expect(headers).toEqual(expect.arrayContaining(['Setting', 'default', 'custom', 'trial']));
    // Recording differs between the default and the other two; retention only in trial.
    const recording = within(dialog)
      .getByText(/Meeting Recording ›/)
      .closest('tr');
    expect(within(recording).getAllByText('false')).toHaveLength(2);
    expect(within(recording).getByText('true')).toBeTruthy();
    const retention = within(dialog)
      .getByText(/Retention ›/)
      .closest('tr');
    expect(within(retention).getByText('30')).toBeTruthy();
    expect(within(retention).getAllByText('90')).toHaveLength(2);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Export' }));
    fireEvent.click(await within(dialog).findByText('Export as CSV'));
    expect(click).toHaveBeenCalled();
    const rows = (await created[0].text()).trim().split('\r\n');
    expect(rows[0]).toBe('"Section","Setting","default","custom","trial"');
    expect(rows).toHaveLength(3);
    expect(rows.find((row) => row.startsWith('"recording"'))).toMatch(/"true","false","false"$/);
    click.mockRestore();
    restore();
  });

  it('says a comparison leaves out the unsaved changes to the open profile', async () => {
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    fireEvent.change(within(section('Nova Sonic Voice Assistant')).getByLabelText('Voice ID'), {
      target: { value: 'matthew' },
    });
    selectProfile('custom');
    selectProfile('trial');
    fireEvent.click(screen.getByRole('button', { name: 'Compare profiles (2)' }));
    const dialog = await screen.findByRole('dialog', { name: 'Compare custom, trial' });
    expect(within(dialog).getByText(/Your unsaved changes to custom are not included/)).toBeTruthy();
    expect(await within(dialog).findByText('Setting that differs')).toBeTruthy();
  });

  it('creates a profile as a copy of the one selected, and opens it', async () => {
    renderEditor();
    await screen.findByText('Transcript Summary');
    selectProfile('trial');
    fireEvent.click(pageButton('Create profile'));
    const dialog = await screen.findByRole('dialog', { name: 'Create a profile' });
    expect(within(dialog).getByLabelText('Profile name').value).toBe('trial-copy');
    fireEvent.change(within(dialog).getByLabelText('Profile description'), { target: { value: 'for the pilot' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await screen.findByText(/Created the trial-copy profile as a copy of trial/)).toBeTruthy();
    expect(callsOf('CreateConfigurationProfile')[0][0].variables.input).toEqual({
      Name: 'trial-copy',
      CopyFrom: 'trial',
      Description: 'for the pilot',
    });
    await waitFor(() => expect(callsOf('GetConfiguration').at(-1)[0].variables).toEqual({ Profile: 'trial-copy' }));
  });

  it('refuses a new profile name already in use', async () => {
    renderEditor();
    await screen.findByText('Transcript Summary');
    fireEvent.click(pageButton('Create profile'));
    const dialog = await screen.findByRole('dialog', { name: 'Create a profile' });
    fireEvent.change(within(dialog).getByLabelText('Profile name'), { target: { value: 'Trial' } });
    expect(within(dialog).getByText('A profile with that name already exists.')).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: 'Create' }).disabled).toBe(true);
  });

  it('imports an exported file as a new profile, holding what the file sets', async () => {
    renderEditor();
    await screen.findByText('Transcript Summary');
    const file = new File(
      [
        JSON.stringify({
          format: 'lma-configuration-profile',
          formatVersion: 1,
          profile: 'staging',
          sections: { nova_sonic_config: { voiceId: 'amy' }, retention: {}, not_here: {} },
        }),
      ],
      'staging.json',
      { type: 'application/json' },
    );
    fireEvent.change(screen.getByTestId('profile-import'), { target: { files: [file] } });
    const dialog = await screen.findByRole('dialog', { name: 'Import staging.json as a new profile' });
    expect(within(dialog).getByText(/no such section: not_here/)).toBeTruthy();
    // The name the file was exported from, as it is free.
    expect(within(dialog).getByLabelText('Imported profile name').value).toBe('staging');
    expect(callsOf('CreateConfigurationProfile')).toHaveLength(0);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import' }));
    expect(await screen.findByText(/Created the staging profile from staging.json/)).toBeTruthy();
    const { input } = callsOf('CreateConfigurationProfile')[0][0].variables;
    expect(input.CopyFrom).toBeUndefined();
    expect(input.Comment).toBe('Imported from staging.json');
    expect(JSON.parse(input.Body)).toEqual({ nova_sonic_config: { voiceId: 'amy' }, retention: {} });
    await waitFor(() => expect(callsOf('GetConfiguration').at(-1)[0].variables).toEqual({ Profile: 'staging' }));
  });

  it('opens the revision history of the profile open in the editor', async () => {
    renderEditor({ route: '/configuration/settings?profile=trial' });
    await screen.findByText('Transcript Summary');
    expect(callsOf('ListConfigurationRevisions')).toHaveLength(0);
    fireEvent.click(pageButton('Revisions…'));
    expect(await screen.findByRole('dialog', { name: 'Revision history of trial' })).toBeTruthy();
    await waitFor(() => expect(callsOf('ListConfigurationRevisions')[0][0].variables).toEqual({ Profile: 'trial' }));
  });

  it('opens with the revision history shown when the address asks for it', async () => {
    renderEditor({ route: '/configuration/settings?profile=trial&history=1' });
    expect(await screen.findByRole('dialog', { name: 'Revision history of trial' })).toBeTruthy();
    await waitFor(() => expect(callsOf('ListConfigurationRevisions')[0][0].variables).toEqual({ Profile: 'trial' }));
  });
});

describe('the editor actions', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
    profilesMock.mockReset();
    profilesMock.mockResolvedValue(THREE_PROFILES);
  });

  const actionsItem = async (text) => {
    fireEvent.click(screen.getByRole('button', { name: 'Actions' }));
    fireEvent.click(await screen.findByText(text));
  };

  it('saves the editor contents, unsaved edits included, as a new profile, and leaves the open one', async () => {
    graphqlMock.mockImplementation(async (request) => {
      if (/mutation CreateConfigurationProfile/.test(request.query)) {
        return { data: { createConfigurationProfile: { Success: true, Profile: 'pilot', Version: 1, Revision: 1 } } };
      }
      return answerFor(request.variables.Profile, { recording: { recordAudio: false } });
    });
    renderEditor();
    openSection(await screen.findByText('Nova Sonic Voice Assistant'));
    fireEvent.change(within(section('Nova Sonic Voice Assistant')).getByLabelText('Voice ID'), {
      target: { value: 'matthew' },
    });
    await actionsItem('Save current edits as new profile…');
    const dialog = await screen.findByRole('dialog', { name: 'Save current edits as a new profile' });
    fireEvent.change(within(dialog).getByLabelText('New profile name'), { target: { value: 'pilot' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save as new profile' }));
    expect(await screen.findByText(/Created the pilot profile from what the editor held for custom/)).toBeTruthy();
    expect(callsOf('UpdateConfiguration')).toHaveLength(0);
    const { input } = callsOf('CreateConfigurationProfile')[0][0].variables;
    expect(input.Name).toBe('pilot');
    expect(input.CopyFrom).toBeUndefined();
    const body = JSON.parse(input.Body);
    // Every section, as a save would store it: the stored change and the unsaved one.
    expect(Object.keys(body).sort()).toEqual(SECTIONS.map((entry) => entry.Name).sort());
    expect(body.recording).toEqual({ recordAudio: false });
    expect(body.nova_sonic_config).toEqual({ voiceId: 'matthew' });
    expect(body.retention).toEqual({});
    await waitFor(() => expect(callsOf('GetConfiguration').at(-1)[0].variables).toEqual({ Profile: 'pilot' }));
  });

  it('resets every section to its defaults on the page only, and saves that as one revision', async () => {
    graphqlMock.mockResolvedValueOnce(
      answer({
        custom: { recording: { recordAudio: false }, nova_sonic_config: { voiceId: 'amy' } },
      }),
    );
    renderEditor();
    await screen.findByText('Transcript Summary');
    await actionsItem('Reset all sections to defaults');
    expect(await screen.findByText(/Every section is set to its defaults on the page/)).toBeTruthy();
    expect(callsOf('UpdateConfiguration')).toHaveLength(0);

    fireEvent.click(pageButton('Save changes'));
    const dialog = await screen.findByRole('dialog', { name: /Save the configuration/ });
    graphqlMock.mockResolvedValueOnce(saved()).mockResolvedValueOnce(answer({ version: 5 }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await screen.findByText(/Saved as revision/);
    expect(JSON.parse(callsOf('UpdateConfiguration')[0][0].variables.input.Sections)).toEqual({
      nova_sonic_config: {},
      recording: {},
    });
  });

  it('reloads the editor after a revision is restored', async () => {
    let restoredYet = false;
    graphqlMock.mockImplementation(async (request) => {
      if (/query ListConfigurationRevisions/.test(request.query)) {
        return {
          data: {
            listConfigurationRevisions: {
              Profile: 'custom',
              Version: 4,
              Revisions: [
                { Revision: 3, CreatedBy: 'second@example.com', Source: 'save', Published: true, Sections: [] },
                { Revision: 2, CreatedBy: 'first@example.com', Source: 'save', Published: false, Sections: [] },
              ],
            },
          },
        };
      }
      if (/mutation RestoreConfigurationRevision/.test(request.query)) {
        restoredYet = true;
        return { data: { restoreConfigurationRevision: { Success: true, Version: 5, Revision: 4, Unchanged: false } } };
      }
      return answer({ custom: restoredYet ? { nova_sonic_config: { voiceId: 'amy' } } : {} });
    });
    renderEditor();
    await screen.findByText('Transcript Summary');
    fireEvent.click(pageButton('Revisions…'));
    const history = await screen.findByRole('dialog', { name: 'Revision history of custom' });
    await within(history).findByText('first@example.com');
    fireEvent.click(within(history).getByRole('checkbox', { name: 'Select revision 2' }));
    fireEvent.click(within(history).getByRole('button', { name: 'Restore' }));
    const confirm = await screen.findByRole('dialog', { name: 'Restore revision 2?' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Restore' }));
    expect(await screen.findByText(/Restored revision 2 as revision 4/)).toBeTruthy();
    await waitFor(() => expect(callsOf('GetConfiguration')).toHaveLength(2));
    expect(callsOf('RestoreConfigurationRevision')[0][0].variables.input).toMatchObject({
      Profile: 'custom',
      Revision: 2,
      ExpectedVersion: 4,
    });
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

  it('send the Configuration History page to the revision history of the profile it named', () => {
    render(
      <MemoryRouter initialEntries={[`${CONFIGURATION_HISTORY_PATH}?profile=trial`]}>
        <Routes>
          {movedConfigurationRoutes()}
          <Route path={`${CONFIGURATION_PATH}/*`} element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText(`${CONFIGURATION_PATH}?profile=trial&history=1`)).toBeTruthy();
  });
});
