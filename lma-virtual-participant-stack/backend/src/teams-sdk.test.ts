import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { endpointFromConnectionString, resolveTeamsLocator } from './teams-sdk.js';
import { resolveJoinMethod } from './details.js';

test('endpointFromConnectionString extracts and trims the endpoint', () => {
    assert.equal(
        endpointFromConnectionString('endpoint=https://r.unitedstates.communication.azure.com/;accesskey=k'),
        'https://r.unitedstates.communication.azure.com',
    );
    assert.equal(
        endpointFromConnectionString('accesskey=k;endpoint=https://r.communication.azure.com'),
        'https://r.communication.azure.com',
    );
    assert.equal(
        endpointFromConnectionString('ENDPOINT=https://r.communication.azure.com/;accesskey=k'),
        'https://r.communication.azure.com',
    );
});

test('endpointFromConnectionString throws when the endpoint segment is missing', () => {
    assert.throws(() => endpointFromConnectionString('accesskey=k'), /missing an endpoint/);
    assert.throws(() => endpointFromConnectionString(''), /missing an endpoint/);
});

test('resolveTeamsLocator uses meetingId for numeric ids', () => {
    assert.deepEqual(resolveTeamsLocator('243574196567966', 'pw'), {
        meetingId: '243574196567966',
        meetingLink: '',
    });
    assert.deepEqual(resolveTeamsLocator('  243 574 196 567 966  ', 'pw'), {
        meetingId: '243574196567966',
        meetingLink: '',
    });
});

test('resolveTeamsLocator passes full join URLs through untouched', () => {
    const meetupJoin =
        'https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%22Tid%22%3a%22x%22%7d';
    assert.deepEqual(resolveTeamsLocator(meetupJoin, undefined), {
        meetingId: '',
        meetingLink: meetupJoin,
    });
    const shortLink = 'https://teams.microsoft.com/meet/9372362162554?p=secret';
    assert.deepEqual(resolveTeamsLocator(shortLink, undefined), {
        meetingId: '',
        meetingLink: shortLink,
    });
});

test('resolveTeamsLocator builds a meeting link for non-numeric slugs', () => {
    assert.deepEqual(resolveTeamsLocator('ht4523003444357', 'pw1'), {
        meetingId: '',
        meetingLink: 'https://teams.microsoft.com/meet/ht4523003444357?p=pw1',
    });
    assert.deepEqual(resolveTeamsLocator('ht4523003444357?p=pw1', 'pw1'), {
        meetingId: '',
        meetingLink: 'https://teams.microsoft.com/meet/ht4523003444357?p=pw1',
    });
    assert.deepEqual(resolveTeamsLocator('ht4523003444357', undefined), {
        meetingId: '',
        meetingLink: 'https://teams.microsoft.com/meet/ht4523003444357',
    });
});

test('resolveTeamsLocator returns empty for blank input', () => {
    assert.deepEqual(resolveTeamsLocator('', undefined), { meetingId: '', meetingLink: '' });
    assert.deepEqual(resolveTeamsLocator('   ', undefined), { meetingId: '', meetingLink: '' });
});

test('resolveJoinMethod honors an explicit override', () => {
    assert.equal(resolveJoinMethod('sdk', false), 'sdk');
    assert.equal(resolveJoinMethod('dom', true), 'dom');
    assert.equal(resolveJoinMethod('SDK', false), 'sdk');
});

test('resolveJoinMethod falls back to credential presence', () => {
    assert.equal(resolveJoinMethod('auto', true), 'sdk');
    assert.equal(resolveJoinMethod('auto', false), 'dom');
    assert.equal(resolveJoinMethod(undefined, true), 'sdk');
    assert.equal(resolveJoinMethod(undefined, false), 'dom');
    assert.equal(resolveJoinMethod('nonsense', false), 'dom');
});
