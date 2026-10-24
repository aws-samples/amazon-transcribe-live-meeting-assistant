/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Changing the assistant's meeting mode and activation mode on a running agent.
 *
 * The meeting mode decides the system prompt and the registered tool set, and
 * both are sent once when the Nova session opens — so the thing that has to work
 * is not a flag flip but a session rebuilt around the new configuration, with
 * the agent's own derived state moved across to match. Translator mode is the
 * sharpest case because it replaces the base prompt instead of adding to it, but
 * every mode change is a rebuild for the same reason.
 *
 * The guards are each asserted separately, because each of them exists to
 * prevent a specific way of making a live meeting worse: applying half a
 * request, tearing down a working session to change nothing, and two changes
 * racing each other through the same teardown.
 *
 * What is NOT covered here: whether the model obeys the new prompt, and how long
 * the gap in the assistant's hearing actually lasts. Both need a live meeting.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { NovaAgent } from './nova-agent.js';
import { ElevenLabsAgent } from './elevenlabs-agent.js';
import { NoOpAgent } from './noop-agent.js';
import { MeetingMode } from './nova-sonic-config-loader.js';
import {
    ASSISTANT_TOGGLE_LOG_TAG,
    AssistantReconfigureRequest,
    assistantToggleChatReply,
} from './assistant-toggles.js';
import { details } from './details.js';

const BASE_PROMPT = 'You are Alex, an AI meeting assistant. Mention the quarterly roadmap.';
const STRANDS_ARN = 'arn:aws:lambda:us-east-1:123456789012:function:strands';

/** Session lifecycle calls the rebuild made, in order. */
type Calls = string[];

/**
 * A Nova agent with no Bedrock client, whose three session lifecycle methods are
 * replaced by recorders — enough to drive a reconfiguration and inspect what it
 * did to the prompt, the tool set and the session, with no network.
 */
function novaAgent(
    overrides: { meetingMode?: MeetingMode; activationMode?: string; strandsLambdaArn?: string } = {},
): { agent: NovaAgent; calls: Calls } {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: BASE_PROMPT,
        activationMode: overrides.activationMode || 'wake_phrase',
        meetingMode: overrides.meetingMode || 'normal',
        strandsLambdaArn: overrides.strandsLambdaArn,
    });
    const calls: Calls = [];
    const inner = agent as any;
    inner.createSession = async () => {
        calls.push('createSession');
        inner.session = liveSession();
    };
    inner.closeSession = async () => {
        calls.push('closeSession');
        inner.session = null;
    };
    inner.refreshSession = async () => {
        calls.push('refreshSession');
        inner.session = liveSession();
    };
    return { agent, calls };
}

function liveSession() {
    return {
        queue: [],
        isActive: true,
        promptName: 'prompt',
        audioContentId: 'audio',
        isPromptStartSent: true,
        isAudioContentStartSent: true,
        sessionStartTime: Date.now(),
        lastRefreshTime: Date.now(),
    };
}

/** Put the agent in the state of one that has been talking to a live session. */
function withLiveSession(agent: NovaAgent): NovaAgent {
    const inner = agent as any;
    inner.session = liveSession();
    inner._isActive = true;
    inner.streamingActive = true;
    return agent;
}

function toolNames(agent: NovaAgent): string[] {
    return (agent as any).buildToolSpecs().map((tool: any) => tool.toolSpec.name);
}

function systemPrompt(agent: NovaAgent): string {
    return (agent as any).buildSystemPromptContent();
}

function chat(over: Partial<AssistantReconfigureRequest> = {}): AssistantReconfigureRequest {
    return { route: 'chat', requestedBy: 'Alice', ...over };
}

// ---------------------------------------------------------------------------
// A request is taken whole or not at all
// ---------------------------------------------------------------------------

test('a meeting mode that is not one of the configured modes is refused', async () => {
    const { agent, calls } = novaAgent({ meetingMode: 'normal' });
    const outcome = await agent.reconfigure(chat({ meetingMode: 'interpreter' as MeetingMode }));

    assert.equal(outcome.result, 'declined');
    assert.deepEqual(outcome.changed, []);
    assert.equal(outcome.meetingModeAfter, 'normal');
    assert.match(outcome.detail || '', /unrecognised meeting mode/);
    assert.deepEqual(calls, [], 'a refused request must not touch the session');
});

test('an activation mode that is not one of the configured modes is refused', async () => {
    const { agent, calls } = novaAgent({ activationMode: 'wake_phrase' });
    const outcome = await agent.reconfigure(chat({ activationMode: 'always' as any }));

    assert.equal(outcome.result, 'declined');
    assert.equal(outcome.activationModeAfter, 'wake_phrase');
    assert.match(outcome.detail || '', /unrecognised activation mode/);
    assert.deepEqual(calls, []);
});

test('a request with one good half and one bad half applies neither', async () => {
    // Applying the half that parsed would leave the assistant in a
    // configuration nobody asked for — a translator still on wake-phrase
    // activation, say, which translates nothing.
    const { agent, calls } = novaAgent({ meetingMode: 'normal', activationMode: 'wake_phrase' });
    const outcome = await agent.reconfigure(
        chat({ meetingMode: 'translator', activationMode: 'whenever' as any }),
    );

    assert.equal(outcome.result, 'declined');
    assert.equal(outcome.meetingModeAfter, 'normal');
    assert.equal((agent as any).meetingMode, 'normal');
    assert.deepEqual(calls, []);
});

test('asking only for the wake phrase is refused in the modes that need to stay listening', async () => {
    // The reverse of the rule the chat commands apply going in. Group mode
    // starts muted and `activate()` does not unmute it; translator mode receives
    // no audio between activations. Either one on wake-phrase activation is
    // silent for the rest of the meeting, so the request is refused with the
    // command that makes it possible rather than quietly changing the meeting
    // mode nobody asked about.
    for (const mode of ['group', 'translator'] as MeetingMode[]) {
        const { agent, calls } = novaAgent({ meetingMode: mode, activationMode: 'always_active' });
        withLiveSession(agent);

        const outcome = await agent.reconfigure(chat({ activationMode: 'wake_phrase' }));

        assert.equal(outcome.result, 'declined', `${mode} must not be left on the wake phrase`);
        assert.equal(outcome.activationModeAfter, 'always_active');
        assert.equal((agent as any).activationMode, 'always_active');
        assert.match(outcome.detail || '', /has to stay always listening/);
        assert.match(outcome.detail || '', /LMA assist/, 'the reply must say how to get there');
        assert.deepEqual(calls, [], 'a refused request must not touch the session');
    }
});

test('switching into a mode that needs listening without asking for it is refused too', async () => {
    // Reached by a route that supplies only the meeting mode — the chat commands
    // for these modes ask for both, but `reconfigure` cannot assume its caller
    // did.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'wake_phrase' });

    const outcome = await agent.reconfigure(chat({ meetingMode: 'translator' }));

    assert.equal(outcome.result, 'declined');
    assert.equal((agent as any).meetingMode, 'normal');

    // Asking for both together is what the chat command does, and it works.
    const both = await agent.reconfigure(chat({ meetingMode: 'translator', activationMode: 'always_active' }));
    assert.equal(both.result, 'applied');
    assert.deepEqual(both.changed, ['meetingMode', 'activationMode']);
});

test('the wake phrase is still available in assistant mode', async () => {
    // The rule must not spread beyond the two modes that need it.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);
    const outcome = await agent.reconfigure(chat({ activationMode: 'wake_phrase' }));
    assert.equal(outcome.result, 'applied');
});

test('a mode change does not close an activation window somebody opened', async () => {
    // On wake-phrase activation a session exists only because the assistant was
    // addressed. Closing it would leave the window counting down against no
    // session, so the assistant would be deaf for the rest of it.
    const { agent, calls } = novaAgent({ meetingMode: 'group', activationMode: 'wake_phrase' });
    withLiveSession(agent);
    (agent as any)._isActivated = true;

    const outcome = await agent.reconfigure(chat({ meetingMode: 'normal' }));

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, true);
    assert.deepEqual(calls, ['refreshSession'], 'the window must be replaced, not closed');
    assert.equal(agent.isActivated(), true);
});

test('only the wake phrase mode gives up the session it is holding', async () => {
    // The no-session-between-activations contract belongs to `wake_phrase`
    // specifically, not to "anything that is not always_active". `start()`
    // opens a session immediately for every other value, so tearing one down
    // for a mode that expects to keep it would leave the assistant worse off
    // than it started.
    // The request changes only the meeting mode, so the activation mode under
    // test is still in place when the rebuild runs. Asking for an activation
    // mode as well would replace it first and never reach the branch.
    const { agent, calls } = novaAgent({ meetingMode: 'group' });
    withLiveSession(agent);
    (agent as any).activationMode = 'strands_tool';
    (agent as any)._isActivated = false;

    const outcome = await agent.reconfigure(chat({ meetingMode: 'normal' }));

    assert.equal(outcome.result, 'applied');
    assert.equal((agent as any).activationMode, 'strands_tool', 'the activation mode must be untouched');
    assert.deepEqual(calls, ['refreshSession'], 'a mode that holds a session keeps holding one');
});

test('a mode change with no activation window closes the session it finds', async () => {
    // The wake-phrase cost contract: nothing is held open between activations,
    // so a session left over from an activation that has lapsed is torn down
    // rather than replaced.
    //
    // Reached by `LMA assist` on a deployment configured as group mode with
    // wake-phrase activation — the one combination that leaves a wake-phrase
    // agent holding a session it is not activated for.
    const { agent, calls } = novaAgent({ meetingMode: 'group', activationMode: 'wake_phrase' });
    withLiveSession(agent);
    (agent as any)._isActivated = false;

    const outcome = await agent.reconfigure(chat({ meetingMode: 'normal' }));

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, true);
    assert.deepEqual(calls, ['closeSession']);
});

test('a mode change that keeps the assistant listening replaces the session rather than closing it', async () => {
    const { agent, calls } = novaAgent({ meetingMode: 'normal', activationMode: 'wake_phrase' });
    withLiveSession(agent);
    (agent as any)._isActivated = false;

    const outcome = await agent.reconfigure(chat({ meetingMode: 'group', activationMode: 'always_active' }));

    assert.equal(outcome.result, 'applied');
    assert.deepEqual(calls, ['refreshSession']);
});

// ---------------------------------------------------------------------------
// Asking for what it already is costs the meeting nothing
// ---------------------------------------------------------------------------

test('a request for the configuration it is already in rebuilds nothing', async () => {
    // Without this a participant retyping a command, or a button pressed twice,
    // would tear down a working session each time.
    const { agent, calls } = novaAgent({ meetingMode: 'translator', activationMode: 'always_active' });
    withLiveSession(agent);

    const outcome = await agent.reconfigure(chat({ meetingMode: 'translator', activationMode: 'always_active' }));

    assert.equal(outcome.result, 'unchanged');
    assert.deepEqual(outcome.changed, []);
    assert.equal(outcome.sessionRebuilt, false);
    assert.match(outcome.detail || '', /already in the requested configuration/);
    assert.deepEqual(calls, []);
});

test('a request that changes one axis and restates the other changes only the one', async () => {
    const { agent } = novaAgent({ meetingMode: 'translator', activationMode: 'always_active' });
    withLiveSession(agent);
    const outcome = await agent.reconfigure(chat({ meetingMode: 'normal', activationMode: 'always_active' }));

    assert.equal(outcome.result, 'applied');
    assert.deepEqual(outcome.changed, ['meetingMode']);
});

// ---------------------------------------------------------------------------
// The meeting mode: prompt, tools and derived state
// ---------------------------------------------------------------------------

test('switching into translator mode replaces the prompt rather than adding to it', async () => {
    // Translator mode is the reason a mode change cannot be a flag flip: the
    // base prompt is gone, not appended to, so a session built for assistant
    // mode cannot be reused.
    const { agent, calls } = novaAgent({
        meetingMode: 'normal',
        activationMode: 'always_active',
        strandsLambdaArn: STRANDS_ARN,
    });
    withLiveSession(agent);
    assert.match(systemPrompt(agent), /quarterly roadmap/);

    const outcome = await agent.reconfigure(chat({ meetingMode: 'translator', activationMode: 'always_active' }));

    assert.equal(outcome.result, 'applied');
    assert.deepEqual(outcome.changed, ['meetingMode']);
    assert.equal(outcome.sessionRebuilt, true);
    assert.deepEqual(calls, ['refreshSession']);

    const prompt = systemPrompt(agent);
    assert.match(prompt, /TRANSLATOR MODE:/);
    assert.match(prompt, /PAUSE TRIGGER/);
    assert.doesNotMatch(prompt, /quarterly roadmap/, 'the base prompt must be gone, not appended to');
    assert.doesNotMatch(prompt, /INTERRUPTION HANDLING/);
    assert.doesNotMatch(prompt, /STOP REQUESTS/);
    // The meeting-assistant tool is not offered to a translator.
    assert.deepEqual(toolNames(agent), ['mute']);
});

test('switching out of translator mode restores the assistant prompt and tools', async () => {
    const { agent, calls } = novaAgent({
        meetingMode: 'translator',
        activationMode: 'always_active',
        strandsLambdaArn: STRANDS_ARN,
    });
    withLiveSession(agent);

    const outcome = await agent.reconfigure(chat({ meetingMode: 'normal' }));

    assert.equal(outcome.result, 'applied');
    assert.deepEqual(calls, ['refreshSession']);

    const prompt = systemPrompt(agent);
    assert.match(prompt, /quarterly roadmap/);
    assert.match(prompt, /STOP REQUESTS:/);
    assert.match(prompt, /INTERRUPTION HANDLING:/);
    assert.doesNotMatch(prompt, /TRANSLATOR MODE/);
    assert.deepEqual(toolNames(agent), ['strands_agent', 'mute']);
});

test('translator pause state starts from unpaused on the way in and on the way out', async () => {
    // The pause state decides which single tool is registered. Carrying MUTED
    // across a mode change would register `unmute` against a prompt that never
    // mentions it, and the translator would have no way back.
    const { agent } = novaAgent({ meetingMode: 'translator', activationMode: 'always_active' });
    withLiveSession(agent);
    (agent as any)._translatorState = 'MUTED';
    assert.deepEqual(toolNames(agent), ['unmute']);

    await agent.reconfigure(chat({ meetingMode: 'normal' }));
    assert.equal((agent as any)._translatorState, 'UNMUTED');

    await agent.reconfigure(chat({ meetingMode: 'translator', activationMode: 'always_active' }));
    assert.equal((agent as any)._translatorState, 'UNMUTED');
    assert.deepEqual(toolNames(agent), ['mute']);
});

test('a mode reached by toggle starts in the same audible state as one configured up front', async () => {
    // Group is the only mode that starts muted and waits to be addressed.
    // Anything else here would make "LMA group" mean something different from
    // deploying with group mode configured.
    const toGroup = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' }).agent;
    withLiveSession(toGroup);
    await toGroup.reconfigure(chat({ meetingMode: 'group' }));
    assert.equal(toGroup.isMuted(), true);
    assert.equal((toGroup as any).groupMeetingMode, true);

    const fromGroup = novaAgent({ meetingMode: 'group', activationMode: 'always_active' }).agent;
    withLiveSession(fromGroup);
    assert.equal(fromGroup.isMuted(), true);
    await fromGroup.reconfigure(chat({ meetingMode: 'normal' }));
    assert.equal(fromGroup.isMuted(), false);
    assert.equal((fromGroup as any).groupMeetingMode, false);

    const toTranslator = novaAgent({ meetingMode: 'group', activationMode: 'always_active' }).agent;
    withLiveSession(toTranslator);
    await toTranslator.reconfigure(chat({ meetingMode: 'translator' }));
    assert.equal(toTranslator.isMuted(), false);
});

test('the group auto-mute timer does not outlive the mode that armed it', async () => {
    const { agent } = novaAgent({ meetingMode: 'group', activationMode: 'always_active' });
    withLiveSession(agent);
    (agent as any).startConversationSession();
    assert.notEqual((agent as any).conversationTimeout, null);

    await agent.reconfigure(chat({ meetingMode: 'normal' }));

    assert.equal((agent as any).conversationTimeout, null, 'a timer from the old mode would re-mute the new one');
    assert.equal((agent as any).conversationActive, false);
    assert.equal((agent as any).autoMuteQueued, false);
});

test('a mode change does not replay the old mode conversation into the new prompt', async () => {
    // The next session is built for the new mode. Replaying turns from the old
    // one primes it with a conversation that mode never had — most visibly a
    // translator opening by answering the last question the assistant was asked.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);
    (agent as any).addToConversationHistory('USER', 'what did we decide about hiring?');
    (agent as any).addToConversationHistory('ASSISTANT', 'you deferred it to next quarter');
    assert.equal((agent as any).conversationHistory.length, 2);

    await agent.reconfigure(chat({ meetingMode: 'translator' }));
    assert.deepEqual((agent as any).conversationHistory, []);
});

test('an activation-only change does not discard the conversation it was having', async () => {
    // Same assistant, same prompt, same mode — only when it is listening
    // changed, so the conversation so far is still its own. It is kept, and
    // replayed into the next session the agent refreshes; a session opened from
    // nothing starts without the replay, which is `setupInitialEvents`'
    // behaviour and not something this change alters.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'wake_phrase' });
    withLiveSession(agent);
    (agent as any).addToConversationHistory('USER', 'what did we decide about hiring?');

    await agent.reconfigure(chat({ activationMode: 'always_active' }));
    assert.equal((agent as any).conversationHistory.length, 1);
});

// ---------------------------------------------------------------------------
// The activation mode: whether a session is held open at all
// ---------------------------------------------------------------------------

test('switching to always listening opens a session straight away', async () => {
    // On wake-phrase activation the agent holds no session between
    // activations, so there is nothing to refresh — it has to start listening
    // now rather than at the next wake phrase.
    const { agent, calls } = novaAgent({ activationMode: 'wake_phrase' });
    assert.equal(agent.isActivated(), false);

    const outcome = await agent.reconfigure(chat({ activationMode: 'always_active' }));

    assert.equal(outcome.result, 'applied');
    assert.deepEqual(outcome.changed, ['activationMode']);
    assert.equal(outcome.sessionRebuilt, true);
    assert.equal(agent.isActivated(), true);
    assert.deepEqual(calls, ['createSession']);
});

test('switching to the wake phrase closes the session it was holding open', async () => {
    const { agent, calls } = novaAgent({ activationMode: 'always_active' });
    withLiveSession(agent);
    assert.equal(agent.isActivated(), true);

    const outcome = await agent.reconfigure(chat({ activationMode: 'wake_phrase' }));

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, true);
    assert.equal(agent.isActivated(), false);
    assert.deepEqual(calls, ['closeSession']);
});

test('a mode change on a wake-phrase agent with no session waits for the next wake phrase', async () => {
    // Nothing to tear down and nothing to hold open: the new value is on the
    // agent and the session built by the next activation carries it.
    //
    // Starting from group mode on wake-phrase activation, which is the
    // combination a deployment can be configured into and the UI warns about —
    // and `LMA assist` is the way out of it mid-meeting.
    const { agent, calls } = novaAgent({ meetingMode: 'group', activationMode: 'wake_phrase' });

    const outcome = await agent.reconfigure(chat({ meetingMode: 'normal' }));

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, false);
    assert.equal((agent as any).meetingMode, 'normal');
    assert.equal(agent.isMuted(), false, 'leaving group mode must make it audible');
    assert.deepEqual(calls, []);
});

test('the activation window the old mode was counting down is dropped whatever the new mode is', async () => {
    const { agent } = novaAgent({ activationMode: 'wake_phrase' });
    withLiveSession(agent);
    await agent.activate(600);
    assert.notEqual((agent as any).activationTimeout, null);

    try {
        // Dropped on the way to always_active, not only on the way back. A timer
        // left armed there happens to do nothing because `deactivate()` returns
        // early in that mode, which is not a reason worth relying on.
        await agent.reconfigure(chat({ activationMode: 'always_active' }));
        assert.equal((agent as any).activationTimeout, null, 'switching to always listening drops the window');

        await agent.reconfigure(chat({ activationMode: 'wake_phrase' }));
        assert.equal((agent as any).activationTimeout, null, 'switching back drops it too');
    } finally {
        // A window that survived would hold the event loop open for ten
        // minutes, so clear it whichever assertion failed: these should fail,
        // not hang.
        const surviving = (agent as any).activationTimeout;
        if (surviving) clearTimeout(surviving);
    }
});

// ---------------------------------------------------------------------------
// Two changes cannot race through the same teardown
// ---------------------------------------------------------------------------

test('a second mode change arriving mid-rebuild is refused rather than interleaved', async () => {
    // Both would be tearing down and rebuilding the same session. The second
    // one is told so; the participant can retype it once the first has landed.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);

    let second: ReturnType<NovaAgent['reconfigure']> | null = null;
    (agent as any).refreshSession = async () => {
        second = agent.reconfigure(chat({ meetingMode: 'group' }));
        await second;
    };

    const first = await agent.reconfigure(chat({ meetingMode: 'translator' }));
    assert.equal(first.result, 'applied');

    const secondOutcome = await second!;
    assert.equal(secondOutcome.result, 'declined');
    assert.match(secondOutcome.detail || '', /already in progress/);
    assert.equal((agent as any).meetingMode, 'translator', 'the first change must stand');
});

test('a periodic refresh already queued is superseded by the rebuild', async () => {
    // The queued refresh was going to produce a fresh session; this rebuild
    // produces it instead. Leaving the flag set would tear the new session down
    // again as soon as the agent next went idle.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);
    (agent as any).refreshQueued = true;

    await agent.reconfigure(chat({ meetingMode: 'group' }));

    assert.equal((agent as any).refreshQueued, false);
});

test('a refresh that will not settle is left to finish, with a rebuild queued behind it', async () => {
    // refreshSession declines to start a second teardown, so claiming a rebuild
    // here would be reporting one that never happened. If that refresh had
    // already sent its promptStart, the live session is running the old mode's
    // prompt and tools — so a rebuild is queued, and the existing idle-refresh
    // machinery converges the session at the next idle moment rather than
    // leaving it to the four-minute timer.
    const { agent, calls } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);
    (agent as any).isRefreshing = true;
    (agent as any).RECONFIGURE_REFRESH_WAIT_MS = 50;
    // Recorded rather than called through, so the assertion is about this path
    // starting the machinery and no 2-second retry timer is left behind.
    const started: string[] = [];
    (agent as any).checkAndExecuteQueuedRefresh = () => started.push('checkAndExecuteQueuedRefresh');

    const outcome = await agent.reconfigure(chat({ meetingMode: 'group' }));

    assert.equal(outcome.result, 'applied', 'the new values are on the agent either way');
    assert.equal(outcome.sessionRebuilt, false);
    assert.deepEqual(calls, []);
    assert.equal((agent as any).refreshQueued, true, 'the session must still converge on the new mode');

    // Setting the flag is not enough on its own: it is acted on only when the
    // assistant finishes speaking or when the four-minute timer fires, and a
    // group-mode assistant starts muted and so may not speak for a long time.
    assert.deepEqual(started, ['checkAndExecuteQueuedRefresh'], 'the queue must be started, not just set');

    // And the participant is not told it is finished while the live session is
    // still the one the agent has left.
    assert.equal(outcome.detail, 'the assistant will finish switching shortly');
    assert.deepEqual(assistantToggleChatReply(outcome), [
        'Live Meeting Assistant: switched to group mode, the assistant will finish switching shortly.',
    ]);
});

test('a change that needed no rebuild is reported as finished, with no caveat', async () => {
    // The deferred case above must not leak into the ordinary one: on the
    // wake-phrase path with no session there is nothing outstanding.
    const { agent } = novaAgent({ meetingMode: 'group', activationMode: 'wake_phrase' });

    const outcome = await agent.reconfigure(chat({ meetingMode: 'normal' }));

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, false);
    assert.equal(outcome.detail, undefined);
    assert.deepEqual(assistantToggleChatReply(outcome), ['Live Meeting Assistant: switched to assistant mode.']);
});

test('a queued refresh is not spent on a refresh already in flight', async () => {
    // checkAndExecuteQueuedRefresh clears the flag and then calls
    // refreshSession, which declines to start a second teardown — so without
    // this the queued refresh would be dropped and the session would stay on the
    // old mode's prompt until the periodic timer came round.
    const { agent, calls } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);
    (agent as any).isRefreshing = true;
    (agent as any).refreshQueued = true;

    (agent as any).checkAndExecuteQueuedRefresh();
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.deepEqual(calls, [], 'nothing may be rebuilt while a refresh is in flight');
    assert.equal((agent as any).refreshQueued, true, 'the queued refresh must survive');

    // Leave no retry timer behind for the rest of the suite.
    (agent as any).refreshQueued = false;
});

test('a rebuild that fails still answers the participant and leaves one log line', async () => {
    // The new values are on the agent by the time the rebuild runs, so a failure
    // is a session left behind rather than a request that did not happen.
    // Letting it escape would reject the promise a platform chat handler is
    // awaiting, and write no toggle line at all.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);
    let attempts = 0;
    (agent as any).refreshSession = async () => {
        attempts += 1;
        throw new Error('bidirectional stream refused');
    };

    const originalLog = console.log;
    const lines: string[] = [];
    console.log = (...args: unknown[]) => {
        const line = args.map(String).join(' ');
        if (line.includes(ASSISTANT_TOGGLE_LOG_TAG)) lines.push(line);
    };
    let outcome;
    try {
        outcome = await agent.reconfigure(chat({ meetingMode: 'translator' }));
    } finally {
        console.log = originalLog;
    }

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, false);
    assert.equal((agent as any).meetingMode, 'translator');
    assert.equal(lines.length, 1, 'one attempt, one line');
    assert.match(lines[0], /session=kept/);
    assert.match(lines[0], /detail="the assistant will finish switching shortly"/);

    // And the session is queued to catch up rather than left on the old prompt:
    // the failure path hands it to the same idle-refresh machinery, which makes
    // one more attempt and then leaves it to the periodic refresh.
    assert.equal(attempts, 2, 'the failed rebuild must be queued for another go');
    assert.equal((agent as any).refreshQueued, false, 'no retry timer left behind');

    // The participant is told it landed, and that it is still settling.
    assert.deepEqual(assistantToggleChatReply(outcome), [
        'Live Meeting Assistant: switched to translator mode, the assistant will finish switching shortly.',
    ]);
});

test('a refresh that produced no session is reported as still switching, not as rebuilt', async () => {
    // `refreshSession` catches its own failures: it schedules a retry a second
    // later and returns normally rather than throwing, so the catch in
    // `reconfigure` never sees them. Reporting from the return alone would log
    // `session=rebuilt` and confirm the switch with no caveat for a mode change
    // that left the agent with nothing.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);
    (agent as any).refreshSession = async () => {
        // What the real method leaves behind when its own rebuild fails.
        (agent as any).session = null;
        (agent as any)._isActive = false;
    };

    const originalLog = console.log;
    const lines: string[] = [];
    console.log = (...args: unknown[]) => {
        const line = args.map(String).join(' ');
        if (line.includes(ASSISTANT_TOGGLE_LOG_TAG)) lines.push(line);
    };
    let outcome;
    try {
        outcome = await agent.reconfigure(chat({ meetingMode: 'group' }));
    } finally {
        console.log = originalLog;
    }

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, false, 'a refresh that produced nothing is not a rebuild');
    assert.equal(outcome.detail, 'the assistant will finish switching shortly');
    assert.equal(lines.length, 1);
    assert.match(lines[0], /session=kept/);
    assert.deepEqual(assistantToggleChatReply(outcome), [
        'Live Meeting Assistant: switched to group mode, the assistant will finish switching shortly.',
    ]);
});

test('a refresh that produced a session is reported as rebuilt', async () => {
    // The guard must not report every refresh as incomplete.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);

    const outcome = await agent.reconfigure(chat({ meetingMode: 'group' }));

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, true);
    assert.equal(outcome.detail, undefined);
});

test('a first session that fails to open is retried, not handed to the refresh path', async () => {
    // The headline case: `LMA translate` on a wake-phrase deployment, where
    // there is no session yet, so the rebuild calls createSession directly.
    // Queueing a refresh cannot recover that — refreshSession declines to act
    // without an active session — and an always_active agent with no session is
    // not recoverable by being addressed either, because activate() returns
    // early in that mode. So the session has to be built again.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'wake_phrase' });
    let attempts = 0;
    (agent as any).createSession = async () => {
        attempts += 1;
        if (attempts === 1) throw new Error('bidirectional stream refused');
        (agent as any).session = liveSession();
    };

    const outcome = await agent.reconfigure(chat({ meetingMode: 'translator', activationMode: 'always_active' }));

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.sessionRebuilt, false);
    assert.equal((agent as any).meetingMode, 'translator');
    assert.equal(
        (agent as any).refreshQueued,
        false,
        'a refresh cannot recover a session that was never opened',
    );

    // The retry is on a one-second timer, matching the recovery refreshSession
    // makes for itself.
    await new Promise((resolve) => setTimeout(resolve, 1100));
    assert.equal(attempts, 2, 'the session must be built again');
    assert.equal((agent as any).session?.isActive, true);
});

test('a teardown that fails on the wake-phrase path promises nothing, because nothing is owed', async () => {
    // Nothing is meant to be open between activations, so there is no recovery
    // to schedule and no caveat to put in the reply: the session the next wake
    // phrase builds carries the new mode.
    const { agent } = novaAgent({ meetingMode: 'group', activationMode: 'wake_phrase' });
    withLiveSession(agent);
    (agent as any)._isActivated = false;
    (agent as any).closeSession = async () => {
        (agent as any).session = null;
        throw new Error('stream already gone');
    };

    const outcome = await agent.reconfigure(chat({ meetingMode: 'normal' }));

    assert.equal(outcome.result, 'applied');
    assert.equal(outcome.detail, undefined);
    assert.equal((agent as any).refreshQueued, false);
    assert.deepEqual(assistantToggleChatReply(outcome), [
        'Live Meeting Assistant: switched to assistant mode.',
    ]);
});

// ---------------------------------------------------------------------------
// What the transcription path is told
// ---------------------------------------------------------------------------

test('the mode the transcription path reads follows the mode the assistant is in', async () => {
    // scribe.ts consults details.meetingMode for every transcript result on the
    // Amazon Transcribe path, to keep the assistant's own translations out of a
    // translator meeting's transcript. A stale value there either lets
    // translations through or — switching the other way — goes on dropping real
    // segments after the meeting has left translator mode.
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);

    await agent.reconfigure(chat({ meetingMode: 'translator' }));
    assert.equal(details.meetingMode, 'translator');

    await agent.reconfigure(chat({ meetingMode: 'normal' }));
    assert.equal(details.meetingMode, 'normal');

    await agent.reconfigure(chat({ meetingMode: 'group' }));
    assert.equal(details.meetingMode, 'group');
});

test('a refused or unchanged request leaves the transcription path where it was', async () => {
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);
    await agent.reconfigure(chat({ meetingMode: 'translator' }));
    assert.equal(details.meetingMode, 'translator');

    await agent.reconfigure(chat({ meetingMode: 'nonsense' as MeetingMode }));
    assert.equal(details.meetingMode, 'translator');

    await agent.reconfigure(chat({ meetingMode: 'translator' }));
    assert.equal(details.meetingMode, 'translator');

    // Leave the shared singleton as the rest of the suite expects to find it.
    await agent.reconfigure(chat({ meetingMode: 'normal' }));
});

// ---------------------------------------------------------------------------
// A tool call in flight belongs to the session that asked for it
// ---------------------------------------------------------------------------

test('a tool result is not delivered into the session a mode change built', async () => {
    // A Strands call takes 5-10 seconds, long enough for a mode change to
    // replace the session underneath it. A toolUseId means nothing to a session
    // that never issued it, and after a switch into translator mode the new
    // session has no such tool at all.
    const { agent } = novaAgent({
        meetingMode: 'normal',
        activationMode: 'always_active',
        strandsLambdaArn: STRANDS_ARN,
    });
    withLiveSession(agent);

    const sent: string[] = [];
    (agent as any).sendToolResult = async (toolUseId: string) => sent.push(toolUseId);
    let releaseTool: () => void = () => {};
    (agent as any).invokeStrandsAgent = async () =>
        new Promise((resolve) => {
            releaseTool = () => resolve({ answer: 'from the old session' });
        });

    const toolCall = (agent as any).processToolUseAsync('tool-use-1', 'strands_agent', {});
    await new Promise((resolve) => setTimeout(resolve, 5));

    // The mode change replaces the session while the tool call is outstanding.
    await agent.reconfigure(chat({ meetingMode: 'translator' }));

    releaseTool();
    await toolCall;
    assert.deepEqual(sent, [], 'the result must not reach a session that never asked for it');
});

test('a tool result is delivered when the session is still the one that asked', async () => {
    // The guard must not swallow the ordinary case.
    const { agent } = novaAgent({
        meetingMode: 'normal',
        activationMode: 'always_active',
        strandsLambdaArn: STRANDS_ARN,
    });
    withLiveSession(agent);

    const sent: string[] = [];
    (agent as any).sendToolResult = async (toolUseId: string) => sent.push(toolUseId);
    (agent as any).invokeStrandsAgent = async () => ({ answer: 'still here' });

    await (agent as any).processToolUseAsync('tool-use-1', 'strands_agent', {});
    assert.deepEqual(sent, ['tool-use-1']);
});

// ---------------------------------------------------------------------------
// The transcript is on a different audio path
// ---------------------------------------------------------------------------

test('a rebuild does not touch the audio streams', async () => {
    // The agent owns exactly one audio process, the pacat stream it plays its
    // own voice into. A rebuild is a Bedrock teardown and nothing else, so even
    // that one is left alone — and the transcript, which is cut from a different
    // sink entirely, has nothing in this path that could interrupt it.
    const audio: string[] = [];
    for (const to of ['translator', 'group', 'normal'] as MeetingMode[]) {
        const from: MeetingMode = to === 'normal' ? 'group' : 'normal';
        const { agent } = novaAgent({ meetingMode: from, activationMode: 'always_active' });
        withLiveSession(agent);
        (agent as any).stopPacatStream = () => audio.push(`stopPacatStream:${to}`);
        (agent as any).ensurePacatStream = () => {
            audio.push(`ensurePacatStream:${to}`);
            return null;
        };

        await agent.reconfigure(chat({ meetingMode: to }));
    }
    assert.equal(audio.length, 0, `a mode change touched the audio stream: ${audio.join(', ')}`);

    const { agent } = novaAgent({ activationMode: 'always_active' });
    withLiveSession(agent);
    (agent as any).stopPacatStream = () => audio.push('stopPacatStream:activation');
    await agent.reconfigure(chat({ activationMode: 'wake_phrase' }));
    assert.equal(audio.length, 0, `an activation change touched the audio stream: ${audio.join(', ')}`);
});

// ---------------------------------------------------------------------------
// Every attempt leaves one record
// ---------------------------------------------------------------------------

test('each attempt logs exactly one toggle line, whether or not it landed', async () => {
    const { agent } = novaAgent({ meetingMode: 'normal', activationMode: 'always_active' });
    withLiveSession(agent);

    const originalLog = console.log;
    const lines: string[] = [];
    console.log = (...args: unknown[]) => {
        const line = args.map(String).join(' ');
        if (line.includes(ASSISTANT_TOGGLE_LOG_TAG)) lines.push(line);
    };
    try {
        await agent.reconfigure(chat({ meetingMode: 'translator' }));
        await agent.reconfigure(chat({ meetingMode: 'translator' }));
        await agent.reconfigure(chat({ meetingMode: 'nonsense' as MeetingMode }));
    } finally {
        console.log = originalLog;
    }

    assert.equal(lines.length, 3);
    assert.match(lines[0], /route=chat by="Alice" result=applied meetingMode=normal->translator/);
    assert.match(lines[1], /result=unchanged/);
    assert.match(lines[2], /result=declined .*detail="unrecognised meeting mode/);
    for (const line of lines) {
        assert.match(line, /vp=\S+/, 'a toggle must name the VP it happened on');
    }
});

// ---------------------------------------------------------------------------
// The providers that cannot do this
// ---------------------------------------------------------------------------

test('a meeting with no voice assistant reports a mode change as not made', async () => {
    const outcome = await new NoOpAgent().reconfigure(chat({ meetingMode: 'translator' }));
    assert.equal(outcome.result, 'declined');
    assert.equal(outcome.sessionRebuilt, false);
    assert.match(outcome.detail || '', /not enabled/);
});

test('the ElevenLabs provider reports a mode change as not made and stays where it was', async () => {
    // Its prompt and tools live in the ElevenLabs dashboard, so LMA has nothing
    // to rebuild a session around. Moving only the mute gating and the
    // wake-phrase handling would leave the agent following instructions for the
    // mode it is no longer in.
    const agent = new ElevenLabsAgent({ apiKey: 'test-key', meetingMode: 'normal', activationMode: 'wake_phrase' });
    const outcome = await agent.reconfigure(chat({ meetingMode: 'translator', activationMode: 'always_active' }));

    assert.equal(outcome.result, 'declined');
    assert.equal(outcome.meetingModeAfter, 'normal');
    assert.equal(outcome.activationModeAfter, 'wake_phrase');
    assert.match(outcome.detail || '', /cannot change mode during a meeting/);
    assert.equal((agent as any).meetingMode, 'normal');
    assert.equal((agent as any).activationMode, 'wake_phrase');
});
