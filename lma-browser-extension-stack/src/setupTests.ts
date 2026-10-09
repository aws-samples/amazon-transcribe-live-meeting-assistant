/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
// jest-dom adds custom jest matchers for asserting on DOM nodes.
// allows you to do things like:
// expect(element).toHaveTextContent(/react/i)
// learn more: https://github.com/testing-library/jest-dom
import '@testing-library/jest-dom';
import { TextDecoder, TextEncoder } from 'util';

// jsdom at the version react-scripts pins does not expose TextEncoder or
// TextDecoder, which every browser this extension runs in does. Node's
// implementations are the same WHATWG API, so the code under test can use the
// globals it will really have — without them, reading non-ASCII text out of a
// base64url payload could not be tested at all.
if (typeof global.TextEncoder === 'undefined') {
  global.TextEncoder = TextEncoder as unknown as typeof global.TextEncoder;
}
if (typeof global.TextDecoder === 'undefined') {
  global.TextDecoder = TextDecoder as unknown as typeof global.TextDecoder;
}
