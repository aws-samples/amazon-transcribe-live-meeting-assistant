/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const fetchAuthSession = vi.fn();
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: (...args) => fetchAuthSession(...args) }));

/* eslint-disable import/first */
import { fetchSessionNamingRole, resetRoleRefreshForTests } from './use-current-session-creds';
/* eslint-enable import/first */

const session = (groups) => ({
  tokens: { accessToken: { payload: groups === undefined ? {} : { 'cognito:groups': groups } } },
  credentials: {},
});

beforeEach(() => {
  fetchAuthSession.mockReset();
  resetRoleRefreshForTests();
});

describe('the session the app starts with', () => {
  it.each([[['User']], [['Admin']], [['Participant']], [['Sales', 'User']]])(
    'is used as it is when its token names a role (%j)',
    async (groups) => {
      fetchAuthSession.mockResolvedValue(session(groups));

      await fetchSessionNamingRole();

      expect(fetchAuthSession).toHaveBeenCalledTimes(1);
      expect(fetchAuthSession).toHaveBeenCalledWith();
    },
  );

  it('is refreshed once when its token names no role, as a token from before the trigger does', async () => {
    const fresh = session(['User']);
    fetchAuthSession.mockResolvedValueOnce(session(undefined)).mockResolvedValueOnce(fresh);

    expect(await fetchSessionNamingRole()).toBe(fresh);
    expect(fetchAuthSession).toHaveBeenLastCalledWith({ forceRefresh: true });
  });

  it('is refreshed at most once per page load', async () => {
    fetchAuthSession.mockResolvedValue(session(undefined));

    await fetchSessionNamingRole();
    await fetchSessionNamingRole();

    expect(fetchAuthSession.mock.calls.filter((call) => call[0]?.forceRefresh)).toHaveLength(1);
  });

  it('is not refreshed when there are no tokens to refresh', async () => {
    fetchAuthSession.mockResolvedValue({});

    await fetchSessionNamingRole();

    expect(fetchAuthSession).toHaveBeenCalledTimes(1);
  });
});
