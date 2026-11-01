/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect, vi } from 'vitest';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

const { isRequestTimeout } = await import('./use-calls-graphql-api');

describe('isRequestTimeout', () => {
  it("recognises AppSync's execution timeout", () => {
    expect(isRequestTimeout({ errors: [{ errorType: 'ExecutionTimeout', message: 'Execution timed out.' }] })).toBe(
      true,
    );
    expect(isRequestTimeout({ errors: [{ message: 'Execution timed out.' }] })).toBe(true);
  });

  it('does not mistake a refusal or a network failure for one', () => {
    expect(isRequestTimeout({ errors: [{ errorType: 'Unauthorized', message: 'Not Authorized' }] })).toBe(false);
    expect(isRequestTimeout(new TypeError('Failed to fetch'))).toBe(false);
    expect(isRequestTimeout(undefined)).toBe(false);
  });
});
