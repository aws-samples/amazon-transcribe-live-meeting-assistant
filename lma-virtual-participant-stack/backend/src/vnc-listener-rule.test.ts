/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The listener rule a Virtual Participant creates for its own live-view path.
 *
 * Two conditions have to hold together: the path selects this participant's own
 * target group, and the origin-verify header establishes that the request arrived
 * through this deployment's own CloudFront distribution -- the same condition the
 * AI stack template puts on the load balancer's own rule. Because this rule is
 * created at run time rather than by CloudFormation, a template test cannot see
 * it, so these tests drive the real method with stubbed AWS clients and inspect
 * the commands it sent.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { VirtualParticipantStatusManager, buildListenerRuleConditions } from './status-manager.js';

const VP_ID = 'vp-test-1234';
const PATH = `/vnc/${VP_ID}`;
const HEADER_NAME = 'x-lma-vnc-origin-verify';
const HEADER_VALUE = 'generated32charvalue';
const LISTENER_ARN = 'arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/vnc/a/b';
const SECRET_ARN = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:origin-verify';
const TARGET_GROUP_ARN = 'arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/vnc/1';

/** Records every command sent, and answers from a scripted queue. */
class RecordingClient {
    public sent: any[] = [];
    constructor(private responder: (command: any) => any = () => ({})) {}
    async send(command: any): Promise<any> {
        this.sent.push(command);
        return this.responder(command);
    }
    named(name: string): any[] {
        return this.sent.filter((c) => c.constructor.name === name);
    }
}

const secretPayload = (value: string) =>
    JSON.stringify({ header_value: 'vnc_origin_verify', headersecret: value });

interface Harness {
    manager: any;
    elb: RecordingClient;
    secrets: RecordingClient;
}

/**
 * A manager wired to stubs. The clients are constructed in the constructor, so
 * they are replaced afterwards; nothing here reaches AWS.
 */
const harness = (options: {
    secretString?: string | null;
    secretThrows?: boolean;
    existingRules?: any[];
    createReturnsArn?: string | null;
} = {}): Harness => {
    process.env.ALB_LISTENER_ARN = LISTENER_ARN;
    process.env.VNC_ORIGIN_VERIFY_SECRET_ARN = SECRET_ARN;
    process.env.VNC_ORIGIN_VERIFY_HEADER_NAME = HEADER_NAME;
    process.env.VPC_ID = 'vpc-123';

    const elb = new RecordingClient((command) => {
        const name = command.constructor.name;
        if (name === 'DescribeRulesCommand') {
            return { Rules: options.existingRules ?? [] };
        }
        if (name === 'CreateRuleCommand') {
            const arn =
                options.createReturnsArn === undefined
                    ? 'arn:aws:elasticloadbalancing:us-east-1:123456789012:listener-rule/x'
                    : options.createReturnsArn;
            return arn ? { Rules: [{ RuleArn: arn }] } : { Rules: [] };
        }
        return {};
    });

    const secrets = new RecordingClient(() => {
        if (options.secretThrows) {
            throw new Error('AccessDeniedException');
        }
        const secretString =
            options.secretString === undefined ? secretPayload(HEADER_VALUE) : options.secretString;
        return secretString === null ? {} : { SecretString: secretString };
    });

    const manager: any = new VirtualParticipantStatusManager(VP_ID);
    manager.elbClient = elb;
    manager.secretsClient = secrets;
    return { manager, elb, secrets };
};

const createRuleInput = (elb: RecordingClient) => {
    const created = elb.named('CreateRuleCommand');
    assert.equal(created.length, 1, 'expected exactly one rule to be created');
    return created[0].input;
};

const headerCondition = (conditions: any[]) =>
    conditions.find((c: any) => c.Field === 'http-header');

// ---------------------------------------------------------------------------
// The conditions the rule carries
// ---------------------------------------------------------------------------

test('the rule requires both the path and the origin-verify header', async () => {
    const { manager, elb } = harness();
    const ruleArn = await manager.createListenerRule(TARGET_GROUP_ARN);
    assert.ok(ruleArn, 'the rule should have been created');

    const conditions = createRuleInput(elb).Conditions;
    const fields = conditions.map((c: any) => c.Field).sort();
    assert.deepEqual(fields, ['http-header', 'path-pattern']);

    const path = conditions.find((c: any) => c.Field === 'path-pattern');
    assert.deepEqual(path.Values, [PATH]);

    const header = headerCondition(conditions);
    assert.equal(header.HttpHeaderConfig.HttpHeaderName, HEADER_NAME);
    assert.deepEqual(header.HttpHeaderConfig.Values, [HEADER_VALUE]);
});

test('the header name comes from the environment, not a hard-coded string', async () => {
    // The name is published by the AI stack; a copy here could drift from it and
    // the rule would silently never match.
    process.env.VNC_ORIGIN_VERIFY_HEADER_NAME = 'x-something-else';
    const { manager, elb } = harness();
    process.env.VNC_ORIGIN_VERIFY_HEADER_NAME = 'x-something-else';
    await manager.createListenerRule(TARGET_GROUP_ARN);
    assert.equal(
        headerCondition(createRuleInput(elb).Conditions).HttpHeaderConfig.HttpHeaderName,
        'x-something-else',
    );
});

test('the rule still forwards to this participant own target group', async () => {
    const { manager, elb } = harness();
    await manager.createListenerRule(TARGET_GROUP_ARN);
    const input = createRuleInput(elb);
    assert.equal(input.Actions[0].Type, 'forward');
    assert.equal(input.Actions[0].TargetGroupArn, TARGET_GROUP_ARN);
});

test('the rule priority is unchanged by adding the header condition', async () => {
    // Ordering is load-bearing: the load balancer own rule sits at priority
    // 50000 so it is evaluated last, and these per-participant rules must keep
    // their 1000-49999 range and their relative order.
    const { manager, elb } = harness();
    const expected = manager.generateRulePriority(VP_ID);
    await manager.createListenerRule(TARGET_GROUP_ARN);
    const priority = createRuleInput(elb).Priority;
    assert.equal(priority, expected);
    assert.ok(priority >= 1000 && priority < 50000, `priority ${priority} outside the range`);
});

test('the participant tag is still applied, which is how cleanup finds the rule', async () => {
    const { manager, elb } = harness();
    await manager.createListenerRule(TARGET_GROUP_ARN);
    const tags = createRuleInput(elb).Tags;
    assert.ok(tags.some((t: any) => t.Key === 'VirtualParticipantId' && t.Value === VP_ID));
});

// ---------------------------------------------------------------------------
// Failing closed
// ---------------------------------------------------------------------------

test('no rule is created when the header value cannot be read', async () => {
    // The whole point of the condition is that a rule never exists without it, so
    // an unreadable value must stop rule creation rather than omit the condition.
    for (const options of [
        { secretThrows: true },
        { secretString: null },
        { secretString: '{"header_value":"vnc_origin_verify"}' },
        { secretString: 'not json' },
    ]) {
        const { manager, elb } = harness(options);
        const result = await manager.createListenerRule(TARGET_GROUP_ARN);
        assert.equal(result, null, `${JSON.stringify(options)} should not produce a rule`);
        assert.equal(
            elb.named('CreateRuleCommand').length,
            0,
            `${JSON.stringify(options)} created a rule anyway`,
        );
    }
});

test('no rule is created when the secret ARN or header name is absent', async () => {
    for (const missing of ['VNC_ORIGIN_VERIFY_SECRET_ARN', 'VNC_ORIGIN_VERIFY_HEADER_NAME']) {
        const { manager, elb } = harness();
        delete process.env[missing];
        const result = await manager.createListenerRule(TARGET_GROUP_ARN);
        assert.equal(result, null, `${missing} absent should not produce a rule`);
        assert.equal(elb.named('CreateRuleCommand').length, 0);
    }
});

test('the value is read once and reused for the life of the task', async () => {
    const { manager, secrets } = harness();
    await manager.createListenerRule(TARGET_GROUP_ARN);
    await manager.createListenerRule(TARGET_GROUP_ARN);
    assert.equal(secrets.named('GetSecretValueCommand').length, 1);
});

test('the value is read from the ARN in the environment', async () => {
    const { manager, secrets } = harness();
    await manager.createListenerRule(TARGET_GROUP_ARN);
    assert.equal(secrets.named('GetSecretValueCommand')[0].input.SecretId, SECRET_ARN);
});

// ---------------------------------------------------------------------------
// Replacing a rule left behind by an earlier task
// ---------------------------------------------------------------------------

test('an existing rule for this path is removed before the new one is created', async () => {
    // A rule created by an earlier image carries only the path condition, and it
    // outlives its task if the manager cleanup never ran. Replacing means the rule
    // in force is always the one this code built.
    const stale = {
        RuleArn: 'arn:aws:elasticloadbalancing:us-east-1:123456789012:listener-rule/stale',
        IsDefault: false,
        Conditions: [{ Field: 'path-pattern', Values: [PATH] }],
    };
    const { manager, elb } = harness({ existingRules: [stale] });
    await manager.createListenerRule(TARGET_GROUP_ARN);

    const deleted = elb.named('DeleteRuleCommand');
    assert.equal(deleted.length, 1);
    assert.equal(deleted[0].input.RuleArn, stale.RuleArn);
    // And the replacement was still created.
    assert.equal(elb.named('CreateRuleCommand').length, 1);
});

test('rules for other participants and the default rule are left alone', async () => {
    const { manager, elb } = harness({
        existingRules: [
            {
                RuleArn: 'arn:aws:elasticloadbalancing:us-east-1:123456789012:listener-rule/other',
                IsDefault: false,
                Conditions: [{ Field: 'path-pattern', Values: ['/vnc/vp-someone-else'] }],
            },
            {
                RuleArn: 'arn:aws:elasticloadbalancing:us-east-1:123456789012:listener-rule/default',
                IsDefault: true,
                Conditions: [],
            },
        ],
    });
    await manager.createListenerRule(TARGET_GROUP_ARN);
    assert.equal(elb.named('DeleteRuleCommand').length, 0);
});

test('a failure while checking for an existing rule does not omit the condition', async () => {
    const elb = new RecordingClient((command) => {
        if (command.constructor.name === 'DescribeRulesCommand') {
            throw new Error('Throttling');
        }
        return { Rules: [{ RuleArn: 'arn:x' }] };
    });
    process.env.ALB_LISTENER_ARN = LISTENER_ARN;
    process.env.VNC_ORIGIN_VERIFY_SECRET_ARN = SECRET_ARN;
    process.env.VNC_ORIGIN_VERIFY_HEADER_NAME = HEADER_NAME;
    const manager: any = new VirtualParticipantStatusManager(VP_ID);
    manager.elbClient = elb;
    manager.secretsClient = new RecordingClient(() => ({
        SecretString: secretPayload(HEADER_VALUE),
    }));

    await manager.createListenerRule(TARGET_GROUP_ARN);
    const conditions = createRuleInput(elb).Conditions;
    assert.ok(headerCondition(conditions), 'the header condition must still be present');
});

// ---------------------------------------------------------------------------
// The pure builder
// ---------------------------------------------------------------------------

test('buildListenerRuleConditions always emits both conditions', () => {
    const conditions = buildListenerRuleConditions(PATH, HEADER_NAME, HEADER_VALUE);
    assert.equal(conditions.length, 2);
    assert.deepEqual(
        conditions.map((c: any) => c.Field).sort(),
        ['http-header', 'path-pattern'],
    );
});

test('buildListenerRuleConditions matches one exact path, not a prefix', () => {
    // An ALB path-pattern value without a wildcard matches that path only, which is
    // what keeps one participant rule from answering for another.
    const conditions = buildListenerRuleConditions(PATH, HEADER_NAME, HEADER_VALUE);
    const path: any = conditions.find((c: any) => c.Field === 'path-pattern');
    assert.deepEqual(path.Values, [PATH]);
    assert.ok(!path.Values[0].includes('*'), 'the per-participant path must not be a wildcard');
});
