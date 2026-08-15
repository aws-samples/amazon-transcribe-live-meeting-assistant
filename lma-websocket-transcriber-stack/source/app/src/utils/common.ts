/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
export const posixifyFilename = function (filename: string) {
    // Replace all invalid characters with underscores.
    const regex = /[^a-zA-Z0-9_.]/g;
    const posixFilename = filename.replace(regex, '_');
    // Remove leading and trailing underscores.
    return posixFilename.replace(/^_+/g, '').replace(/_+$/g, '');
};

export const isError = (arg: unknown): arg is Error => (
    arg instanceof Error
);

export const normalizeErrorForLogging = (arg: unknown): string => {
    if (isError(arg)) {
        return JSON.stringify(arg, Object.getOwnPropertyNames(arg));
    } else if (typeof arg === 'string') {
        return arg;
    } else {
        return `Object not extending Error raised. Type: ${typeof arg}`;
    }
};

/**
 * Whether this call's audio should be uploaded when it ends.
 *
 * The web UI never sends the flag, so the deployment parameter has to decide. It
 * must be resolved at START and stored on the session: resolving it only when an
 * END message arrives loses the recording for every call that ends by the socket
 * closing (tab closed, network drop), which is silent because nothing else in the
 * meeting is affected.
 */
export const resolveShouldRecordCall = (
    clientValue: boolean | undefined | null,
    deploymentDefault: boolean
): boolean => (clientValue === undefined || clientValue === null ? deploymentDefault : clientValue);
