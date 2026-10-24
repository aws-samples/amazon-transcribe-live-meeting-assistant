/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Lowercases and reduces everything that is not a letter or digit to a single
 * space, so "@LMA, translate!" and "lma translate" compare equal.
 *
 * The one normalisation shared by every whole-message chat and transcript
 * matcher: the pause/resume commands (details.ts), the assistant-mode commands
 * (assistant-toggles.ts) and the stop-talking phrases (stop-request.ts).
 * Deliberately dependency-free so any of them can import it without a cycle.
 */
export function normalizeChatText(text: string): string {
    return text
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}
