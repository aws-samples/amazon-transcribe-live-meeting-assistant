
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