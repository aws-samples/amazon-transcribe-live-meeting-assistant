/**
 * Site-level camera/microphone permissions for the meeting web clients.
 *
 * `--use-fake-ui-for-media-stream` auto-accepts the browser's own
 * getUserMedia prompt, but it does not change what
 * `navigator.permissions.query({ name: 'microphone' })` reports. On a fresh
 * profile that is `prompt`, and some clients (Teams in particular) respond by
 * showing their own in-page device dialog after the join click, whose most
 * obvious button joins without audio. Granting the permissions at context
 * level makes the query report `granted`, so the client goes straight to its
 * normal device setup.
 *
 * This is granted for every join, independent of the avatar. Granting the
 * permission does not publish anything: each platform's pre-join flow still
 * mutes the microphone and turns the camera off unless the voice assistant or
 * avatar needs them.
 */

/** Origins of the meeting web clients the VP joins through the browser. */
export const MEETING_MEDIA_ORIGINS: readonly string[] = [
    'https://zoom.us',
    'https://app.zoom.us',
    'https://app.chime.aws',
    'https://teams.microsoft.com',
    'https://teams.live.com',
    'https://web.webex.com',
    'https://meet.google.com',
];

/** The subset of a Playwright BrowserContext this module uses. */
export interface PermissionGrantingContext {
    grantPermissions(permissions: string[], options?: { origin?: string }): Promise<void>;
}

/**
 * Grant camera and microphone to every meeting origin. Never throws: a failed
 * grant for one origin is reported through `onError` and the rest still run.
 * Returns the origins that were granted.
 */
export async function grantMeetingMediaPermissions(
    context: PermissionGrantingContext,
    onError?: (origin: string, err: unknown) => void,
    origins: readonly string[] = MEETING_MEDIA_ORIGINS,
): Promise<string[]> {
    const granted: string[] = [];
    for (const origin of origins) {
        try {
            await context.grantPermissions(['camera', 'microphone'], { origin });
            granted.push(origin);
        } catch (err) {
            onError?.(origin, err);
        }
    }
    return granted;
}
