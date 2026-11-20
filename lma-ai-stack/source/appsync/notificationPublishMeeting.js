/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { util } from '@aws-appsync/utils';

/**
 * The first function of the publishMeetingNotification pipeline: checks the
 * notification and reads the meeting it is for.
 *
 * The mutation is IAM only, so only a backend role reaches it; the publisher
 * checks the person it acts for, if any, may see the meeting. Who receives the
 * notification is decided here, from the meeting row: its Owner and SharedWith
 * are stashed for the last function to put on the published message, where
 * subscription.js filters on them. The publisher cannot name recipients, and
 * nothing in the stored notification records them.
 *
 * The functions that follow: notificationPublishRate.js (at most
 * RATE_PER_MINUTE publishes a minute per meeting), notificationPublishDedup.js
 * (finds the notification an earlier publish with the same Category and
 * DedupKey made) and notificationPublishWrite.js (stores and answers).
 */

const LOWER = 'abcdefghijklmnopqrstuvwxyz';
const DIGITS = '0123456789';
const UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const CATEGORY_CHARS = `${LOWER}${DIGITS}_-`;
const NAME_CHARS = `${LOWER}${UPPER}${DIGITS}_.:/-`;

const MAX_CALL_ID = 256;
const MAX_CATEGORY = 32;
const MAX_TYPE = 64;
const MAX_TITLE = 200;
const MAX_BODY = 2000;
const MAX_REFS = 20;
const MAX_REF = 200;
const MAX_DEDUP_KEY = 200;
const MAX_SOURCE = 64;
const MAX_CREATED_BY = 256;
const MAX_ACTIONS = 2;
const MAX_LABEL = 40;
const MAX_PAYLOAD = 2200;
const MAX_COPY_TEXT = 2000;
const MAX_ARG = 200;

// BEGIN notificationMutationActions -- the actions a MUTATION button may name,
// each with the arguments it takes, all of them required. The UI's registry
// (ui/src/components/meeting-notifications/notification-actions.js) holds the
// same names; test_appsync_contract.py holds the two to each other.
const MUTATION_ACTIONS = {
    // Sets one checklist item's status (updateCallChecklistItem), as the
    // signed-in user who presses the button.
    set_checklist_item_status: ['itemId', 'status'],
};
// END notificationMutationActions

// The values an action's argument may take, where they are fixed: a list of
// values, or the characters allowed with the most there may be.
const MUTATION_ACTION_VALUES = {
    set_checklist_item_status: {
        status: ['open', 'covered', 'partial', 'deferred', 'not_applicable'],
        itemId: { chars: `${LOWER}${UPPER}${DIGITS}._:-`, max: 64 },
    },
};

function bad(message) {
    util.error(message, 'BadRequest');
}

function onlyChars(value, allowed) {
    return value.split('').every((character) => allowed.includes(character));
}

function isBlank(value) {
    return value === null || value === undefined || (typeof value === 'string' && value.trim() === '');
}

function checkText(value, field, max, required) {
    if (isBlank(value)) {
        if (required) {
            bad(`${field} is required`);
        }
        return;
    }
    if (typeof value !== 'string') {
        bad(`${field} must be a string`);
    }
    if (value.length > max) {
        bad(`${field} is longer than ${max} characters`);
    }
}

function checkName(value, field, max, allowed) {
    checkText(value, field, max, true);
    if (!onlyChars(value, allowed)) {
        bad(`${field} may hold only the characters ${allowed}`);
    }
}

function isObject(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
}

function checkCopy(payload) {
    if (Object.keys(payload).join(',') !== 'text') {
        bad('A COPY action payload holds text only');
    }
    checkText(payload.text, 'COPY action text', MAX_COPY_TEXT, true);
}

function checkMutation(payload) {
    const name = payload.action;
    // Object.keys, so a name inherited by every object is not an action.
    if (typeof name !== 'string' || !Object.keys(MUTATION_ACTIONS).includes(name)) {
        bad(`A MUTATION action must name one of: ${Object.keys(MUTATION_ACTIONS).join(', ') || 'none yet'}`);
    }
    const keys = Object.keys(payload);
    if (!keys.every((key) => key === 'action' || key === 'args')) {
        bad('A MUTATION action payload holds action and args only');
    }
    const args = payload.args === undefined ? {} : payload.args;
    if (!isObject(args)) {
        bad('A MUTATION action args must be an object');
    }
    const allowed = MUTATION_ACTIONS[name];
    Object.keys(args).forEach((key) => {
        if (!allowed.includes(key)) {
            bad(`Action ${name} does not take ${key}`);
        }
    });
    const fixed = MUTATION_ACTION_VALUES[name] || {};
    allowed.forEach((key) => {
        checkText(args[key], `Action ${name} ${key}`, MAX_ARG, true);
        const rule = fixed[key];
        if (Array.isArray(rule) && !rule.includes(args[key])) {
            bad(`Action ${name} ${key} must be one of: ${rule.join(', ')}`);
        }
        if (rule && !Array.isArray(rule) && (args[key].length > rule.max || !onlyChars(args[key], rule.chars))) {
            bad(`Action ${name} ${key} is not a valid value`);
        }
    });
}

function checkAction(action) {
    checkText(action.Label, 'Action Label', MAX_LABEL, true);
    checkText(action.Payload, 'Action Payload', MAX_PAYLOAD, true);
    const payload = JSON.parse(action.Payload);
    if (!isObject(payload)) {
        bad('An action payload must be a JSON object');
    }
    if (action.Kind === 'COPY') {
        checkCopy(payload);
    } else {
        checkMutation(payload);
    }
}

function checkInput(input) {
    checkText(input.CallId, 'CallId', MAX_CALL_ID, true);
    checkName(input.Category, 'Category', MAX_CATEGORY, CATEGORY_CHARS);
    if (!LOWER.includes(input.Category.substring(0, 1))) {
        bad('Category must start with a lower-case letter');
    }
    checkName(input.Type, 'Type', MAX_TYPE, NAME_CHARS);
    checkText(input.Title, 'Title', MAX_TITLE, false);
    checkText(input.Body, 'Body', MAX_BODY, false);
    checkText(input.Source, 'Source', MAX_SOURCE, true);
    checkText(input.CreatedBy, 'CreatedBy', MAX_CREATED_BY, false);
    if (!isBlank(input.DedupKey)) {
        checkName(input.DedupKey, 'DedupKey', MAX_DEDUP_KEY, NAME_CHARS);
    }
    const refs = input.Refs || [];
    if (refs.length > MAX_REFS) {
        bad(`At most ${MAX_REFS} Refs`);
    }
    refs.forEach((ref) => checkText(ref, 'Each of Refs', MAX_REF, true));
    const actions = input.Actions || [];
    if (actions.length > MAX_ACTIONS) {
        bad(`At most ${MAX_ACTIONS} Actions`);
    }
    actions.forEach(checkAction);
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} the request
 */
export function request(ctx) {
    const { input } = ctx.args;
    checkInput(input);
    const key = `c#${input.CallId}`;
    return {
        operation: 'GetItem',
        key: util.dynamodb.toMapValues({ PK: key, SK: key }),
        consistentRead: true,
    };
}

/**
 * @param {import('@aws-appsync/utils').Context} ctx the context
 * @returns {*} nothing; the meeting's access and expiry are stashed
 */
export function response(ctx) {
    if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
    }
    const meeting = ctx.result;
    if (!meeting) {
        util.error(`Meeting ${ctx.args.input.CallId} was not found`, 'NotFound');
    }
    // UTC with three digits of milliseconds always, so stored times order by
    // time as strings, as listMeetingNotifications.js compares Since with them.
    const now = util.time.epochMilliSecondsToFormatted(
        util.time.nowEpochMilliSeconds(),
        "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'",
        'UTC',
    );
    const notificationId = util.autoId();
    ctx.stash.notificationMeeting = {
        Owner: meeting.Owner === undefined ? null : meeting.Owner,
        SharedWith: meeting.SharedWith === undefined ? null : meeting.SharedWith,
        ExpiresAfter: meeting.ExpiresAfter === undefined ? null : meeting.ExpiresAfter,
    };
    // The row's expiry: the publisher's (the transcript's, for guidance), and
    // never later than the meeting's.
    const asked = ctx.args.input.ExpiresAfter;
    const meetingExpiry = ctx.stash.notificationMeeting.ExpiresAfter;
    let expiresAfter = null;
    if (asked !== null && asked !== undefined && meetingExpiry !== null) {
        expiresAfter = Math.min(asked, meetingExpiry);
    } else if (asked !== null && asked !== undefined) {
        expiresAfter = asked;
    } else {
        expiresAfter = meetingExpiry;
    }
    ctx.stash.notificationExpiresAfter = expiresAfter;
    ctx.stash.notificationNow = now;
    ctx.stash.notificationId = notificationId;
    ctx.stash.notificationSK = `n#${now}#${notificationId}`;
    return null;
}
