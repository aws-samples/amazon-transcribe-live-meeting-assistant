/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
export type HeaderFields = Record<string, string | string[] | undefined>;

export const canonicalizeHeaderFieldValue = (value: string): string => (
    // https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-message-signatures-08#section-2.1
    // https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-messaging-19#section-5.2
    value.trim().replace(/[ \t]*\r\n[ \t]+/g, ' ')
);

export const queryCanonicalizedHeaderField = (headers: HeaderFields, name: string): string | null => {
    const field = headers[name];
    
    return field ? Array.isArray(field) ? field.map(canonicalizeHeaderFieldValue).join(', ') : canonicalizeHeaderFieldValue(field) : null;
};

export const getClientIP = (headers: HeaderFields): string => {
    const xforward = queryCanonicalizedHeaderField(headers, 'x-forwarded-for');
    return xforward ? xforward.split(',').length > 0 ? xforward.split(',')[0] : 'unknown' : 'unknown';

    // if (xforward) {
    //     const ips = xforward.split(',');
    //     if (ips.length > 0) {
    //         return ips[0];
    //     } else {
    //         return 'unknown';
    //     }
    // } else {
    //     return 'unknown';
    // }
};