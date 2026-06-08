/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect, vi } from 'vitest';

import { resolveStatusDescription, VP_STATUS_CONFIG } from './VirtualParticipantDetails';

// VirtualParticipantDetails.jsx calls generateClient() / ConsoleLogger at module
// load; stub aws-amplify so importing the module under test stays side-effect free.
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('aws-amplify/utils', () => ({ ConsoleLogger: class {} }));

describe('resolveStatusDescription', () => {
  it('uses Fargate-specific copy for WAITING_FOR_CAPACITY on Fargate', () => {
    const text = resolveStatusDescription('WAITING_FOR_CAPACITY', 'FARGATE');
    expect(text).toMatch(/Fargate provisions serverless compute/i);
    expect(text).not.toMatch(/EC2|host slot|auto-scaler/i);
  });

  it('uses EC2-specific copy for WAITING_FOR_CAPACITY on EC2', () => {
    const text = resolveStatusDescription('WAITING_FOR_CAPACITY', 'EC2');
    expect(text).toMatch(/EC2 host slot/i);
    expect(text).toMatch(/auto-scaler/i);
  });

  it('uses launch-type-specific INITIALIZING copy', () => {
    expect(resolveStatusDescription('INITIALIZING', 'FARGATE')).toMatch(/Fargate task/i);
    expect(resolveStatusDescription('INITIALIZING', 'EC2')).toMatch(/EC2 host/i);
  });

  it('normalizes launch type casing', () => {
    expect(resolveStatusDescription('WAITING_FOR_CAPACITY', 'fargate')).toMatch(/Fargate/i);
    expect(resolveStatusDescription('WAITING_FOR_CAPACITY', 'ec2')).toMatch(/EC2/i);
  });

  it('falls back to neutral STATUS_CONFIG copy when launch type is unknown', () => {
    const neutral = VP_STATUS_CONFIG.WAITING_FOR_CAPACITY.description;
    expect(resolveStatusDescription('WAITING_FOR_CAPACITY', undefined)).toBe(neutral);
    expect(resolveStatusDescription('WAITING_FOR_CAPACITY', 'GARBAGE')).toBe(neutral);
    expect(neutral).not.toMatch(/EC2|Fargate/i);
  });

  it('falls back to neutral copy for statuses without launch-type variants', () => {
    expect(resolveStatusDescription('JOINING', 'FARGATE')).toBe(VP_STATUS_CONFIG.JOINING.description);
    expect(resolveStatusDescription('JOINING', 'EC2')).toBe(VP_STATUS_CONFIG.JOINING.description);
  });

  it('prefers an explicit backend statusMessage over any default', () => {
    const msg = 'Asked to leave by Jeremy Feldman.';
    expect(resolveStatusDescription('WAITING_FOR_CAPACITY', 'EC2', msg)).toBe(msg);
    expect(resolveStatusDescription('INITIALIZING', 'FARGATE', msg)).toBe(msg);
  });

  it('uses FAILED config description for an unknown status', () => {
    expect(resolveStatusDescription('NOPE', 'EC2')).toBe(VP_STATUS_CONFIG.FAILED.description);
  });
});
