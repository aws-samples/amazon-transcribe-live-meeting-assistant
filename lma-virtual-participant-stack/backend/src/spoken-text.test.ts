/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A Strands agent answer is reduced to plain spoken text before the voice
 * assistant hands it to Nova Sonic.
 *
 * The answers below are shaped like the ones a live meeting returned for a
 * five-day forecast: bold labels, a heading, bullets with pipe separators, a
 * link, and a table. The last tests go through NovaAgent, with the Strands
 * Lambda client stubbed, to check that the lookup result it hands the model is
 * the spoken form.
 *
 * What is NOT covered here: how Nova Sonic reads the result, which needs the
 * live service. That was measured by replaying a live meeting's four lookups
 * in one Nova 2 Sonic session: the fourth, markdown, answer stopped after its
 * first line in 6 runs of 6, and the same answer as plain sentences was spoken
 * in full in 6 of 6.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { NovaAgent } from './nova-agent.js';
import { toSpokenText } from './spoken-text.js';

const MARKUP = /[*#|`[\]]|\n|https?:/;

test('bold labels and paragraphs become sentences with no markup', () => {
    const answer = "Based on the search results, here's the **5-day forecast for Herndon, VA**:\n\n"
        + '**Today:** Sunny, high 71°F, low 44°F. Northwest winds 10 mph with gusts to 25 mph.\n\n'
        + '**Tomorrow (Saturday):** Partly cloudy, high 66°F';
    assert.equal(
        toSpokenText(answer),
        "Based on the search results, here's the 5-day forecast for Herndon, VA: "
            + 'Today: Sunny, high 71°F, low 44°F. Northwest winds 10 mph with gusts to 25 mph. '
            + 'Tomorrow (Saturday): Partly cloudy, high 66°F',
    );
});

test('each heading and list item is a sentence of its own', () => {
    const answer = '**Day 1 - Today:**\n- High: 63°F | Low: 47°F\n- Mostly cloudy in the morning, becoming partly sunny\n'
        + '\n### Summary\n1. Mild temperatures\n2) Rain on **Monday**\n* Light winds';
    assert.equal(
        toSpokenText(answer),
        'Day 1 - Today: High: 63°F, Low: 47°F. Mostly cloudy in the morning, becoming partly sunny. '
            + 'Summary. Mild temperatures. Rain on Monday. Light winds',
    );
});

test('a table is read row by row with its column headings as labels', () => {
    const answer = 'Forecast:\n\n| Day | High | Low |\n|-----|:----:|----:|\n| Monday | 60°F | 48°F |\n| Tuesday | 64°F | |\n\nThat is all.';
    assert.equal(
        toSpokenText(answer),
        'Forecast: Monday, High: 60°F, Low: 48°F. Tuesday, High: 64°F. That is all.',
    );
});

test('links keep their text and bare URLs keep only their host', () => {
    assert.equal(
        toSpokenText('*Source: [National Weather Service](https://forecast.weather.gov/x?y=1)*'),
        'Source: National Weather Service',
    );
    assert.equal(
        toSpokenText('See https://www.weather.gov/lwx/ for more.'),
        'See weather.gov for more.',
    );
});

test('formatting tags are removed, other angle brackets are kept', () => {
    assert.equal(toSpokenText('<b>Today</b> is <em>sunny</em>'), 'Today is sunny');
    // Removal repeats until no formatting tag is left.
    assert.equal(toSpokenText('<sp<span>an>x'), '<sp an>x');
    assert.equal(toSpokenText('<<b>b>bold'), '< b>bold');
    assert.equal(toSpokenText('when x<5 and y>3'), 'when x<5 and y>3');
});

test('inline code, italics and HTML are unwrapped, snake_case is kept', () => {
    assert.equal(
        toSpokenText('Run `make build`, then check _the_ value of my_config_key.<br>Done'),
        'Run make build, then check the value of my_config_key. Done',
    );
});

test('a fenced block keeps its lines and loses the fences', () => {
    assert.equal(toSpokenText('Steps:\n```\nnpm install\nnpm test\n```'), 'Steps: npm install. npm test');
});

test('plain text passes through with whitespace normalised', () => {
    const plain = 'Tomorrow will be sunny with a high of 75 degrees.';
    assert.equal(toSpokenText(plain), plain);
    assert.equal(toSpokenText('  two\nlines  '), 'two lines');
    assert.equal(toSpokenText('One paragraph\n\nand another'), 'One paragraph. and another');
    assert.equal(toSpokenText(''), '');
});

test('a representative full answer leaves no markup behind', () => {
    const answer = "Based on the search results, here's the **5-Day Forecast for Herndon, Virginia**:\n\n"
        + '**Day 1 - Today:**\n- High: 63°F | Low: 47°F\n- Mostly cloudy\n\n---\n\n'
        + '## Summary\n> Expect *mild* temperatures.\n\n| Day | Rain |\n|---|---|\n| 3 | 60% |\n\n'
        + '[More](https://example.com)';
    const spoken = toSpokenText(answer);
    assert.doesNotMatch(spoken, MARKUP);
    assert.match(spoken, /Day 1 - Today: High: 63°F, Low: 47°F\. Mostly cloudy\. Summary\. Expect mild temperatures\. 3, Rain: 60%\. More$/);
});

function novaWithStrandsAnswer(answer: unknown): any {
    const agent = new NovaAgent({
        modelId: 'amazon.nova-2-sonic-v1:0',
        systemPrompt: 'You are Alex, an AI meeting assistant.',
        activationMode: 'always_active',
        strandsLambdaArn: 'arn:aws:lambda:us-east-1:123456789012:function:STRANDS-test-MeetingAssist',
    });
    const a = agent as any;
    a.lambdaClient = {
        send: async () => ({ Payload: new TextEncoder().encode(JSON.stringify(answer)) }),
    };
    return a;
}

test('the voice assistant hands the model the spoken form of a markdown answer', async () => {
    const a = novaWithStrandsAnswer({ message: "Here's the **forecast**:\n\n- **Today:** Sunny\n- **Tomorrow:** Rain" });
    const result = await a.invokeStrandsAgent({ content: JSON.stringify({ query: 'forecast' }) });
    assert.equal(result, "Here's the forecast: Today: Sunny. Tomorrow: Rain");
});

test('a plain answer reaches the model unchanged, and a non-text answer is still JSON', async () => {
    const plain = 'Tomorrow will be sunny.';
    assert.equal(
        await novaWithStrandsAnswer({ message: plain }).invokeStrandsAgent({ content: JSON.stringify({ query: 'q' }) }),
        plain,
    );
    assert.equal(
        await novaWithStrandsAnswer({ message: { rows: 2 } }).invokeStrandsAgent({ content: JSON.stringify({ query: 'q' }) }),
        '{"rows":2}',
    );
});

test('a response with no answer in it reaches the model as the JSON it was', async () => {
    const payload = { errorMessage: 'Task timed out', link: 'https://example.com/a_b_c', note: '**x** | y' };
    assert.equal(
        await novaWithStrandsAnswer(payload).invokeStrandsAgent({ content: JSON.stringify({ query: 'q' }) }),
        JSON.stringify(payload),
    );
});
