/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/*
 * The chat frame's own script (strands-chat.html). A classic script loaded
 * at the end of <body>, after js/render-markdown.js, so its top-level
 * declarations are page globals as they were when the code sat inline.
 * Kept in a file served from this origin so the page runs under a
 * Content-Security-Policy whose script-src is 'self'.
 */
const urlParams = new URLSearchParams(window.location.search);
const callId = urlParams.get("callId");

const chatMessages = document.getElementById('chatMessages');
const messageInput = document.getElementById('messageInput');
const sendButton = document.getElementById('sendButton');

// Conversation history tracking
const conversationHistory = [];
const MAX_HISTORY_MESSAGES = 10; // 5 pairs (user + assistant)

// Thinking steps tracking
const thinkingSteps = new Map();

// Filled by loadButtonConfiguration, which runs at the end of this script.
let buttonConfig = null;

// This page is always served from the same origin as the LMA app that
// frames it, so the postMessage channel is pinned to that origin in both
// directions: outbound messages target PARENT_ORIGIN instead of '*', and
// inbound messages are only acted on when they came from it.
const PARENT_ORIGIN = window.location.origin;

const isFromParent = (event) => event.origin === PARENT_ORIGIN;

// Frames of the shared screen in answers. The assistant shows one as
// ![alt](keyframe:<FrameId>) (see get_meeting_frames);
// withKeyframePlaceholders (js/render-markdown.js) turns each into a
// placeholder image carrying the id, and the page signs a link for it
// (STRANDS_RESOLVE_KEYFRAMES) through the meeting's access check.
// Signed links last an hour; one fetched more than this long ago is
// fetched again rather than shown.
const KEYFRAME_LINK_MS = 50 * 60 * 1000;
const keyframeLinks = new Map();
const freshLink = (frameId) => {
    const link = keyframeLinks.get(frameId);
    return link && Date.now() - link.fetchedAt < KEYFRAME_LINK_MS ? link : null;
};

function renderAssistantMarkdown(text) {
    return renderMarkdownSafe(withKeyframePlaceholders(text));
}

// What the page hosting this frame can do with a frame of the shared
// screen, from its STRANDS_HOST_FEATURES message. The meeting page can
// show the moment in its transcript; the embeddable chat panel cannot,
// and never sends the message.
const hostFeatures = { showInTranscript: false, pinToWhiteboard: false, openWhiteboard: false };

// What the next question is about, when the page asked about something on it
// (STRANDS_ASK_WITH_CONTEXT: a guidance card, a whiteboard panel): shown above
// the composer, and sent with the question as quoted context.
const askContext = document.getElementById('askContext');
const askContextLabel = document.getElementById('askContextLabel');
const askContextExcerpt = document.getElementById('askContextExcerpt');
const MAX_CONTEXT_CHARS = 6000;
const DEFAULT_PLACEHOLDER = messageInput.placeholder;
let pendingContext = null;

function setAskContext(context) {
    pendingContext = context;
    if (!askContext) return;
    if (!context) {
        askContext.hidden = true;
        messageInput.placeholder = DEFAULT_PLACEHOLDER;
        return;
    }
    askContextLabel.textContent = context.label;
    // The excerpt leaves out the first line when the label already says it.
    const [firstLine, ...rest] = context.text.split('\n');
    const labelText = context.label.includes(': ') ? context.label.split(': ').slice(1).join(': ') : context.label;
    askContextExcerpt.textContent = (firstLine.trim() === labelText.trim() ? rest.join(' ') : context.text).trim();
    askContext.hidden = false;
    messageInput.placeholder = context.question
        ? 'Send to ask, or type a question'
        : 'Ask about this...';
    messageInput.focus();
}

document.getElementById('askContextRemove')?.addEventListener('click', () => setAskContext(null));

/** The message sent for a question asked about something on the page. */
function withContext(question, context) {
    // The label and the text come from the meeting (a card's title, a panel's),
    // so both go inside the quoting, which they cannot close: the assistant's
    // fixed instructions treat a <quoted> block of the request as quoted data.
    const unquoted = (value) => value.replace(/<\/?\s*quoted\s*>/gi, '');
    return `${question}\n\nMy question is about this, from the meeting page:\n`
        + `<quoted>\n${unquoted(context.label)}\n\n${unquoted(context.text)}\n</quoted>`;
}

// The whiteboards an answer made or changed, from its tool results.
const WHITEBOARD_CHANGE_TOOLS = ['create_meeting_whiteboard', 'update_meeting_whiteboard'];
const boardsByMessage = new Map();

function noteWhiteboardChange(messageId, step) {
    if (step?.Type !== 'tool_result' || step.Success === false) return;
    if (!WHITEBOARD_CHANGE_TOOLS.includes(step.ToolName) || !step.ToolInput) return;
    let changed = null;
    try {
        changed = JSON.parse(step.ToolInput);
    } catch (error) {
        return;
    }
    if (!changed || typeof changed.whiteboardId !== 'string') return;
    const boards = boardsByMessage.get(messageId) || new Map();
    boards.set(changed.whiteboardId, typeof changed.whiteboardTitle === 'string' ? changed.whiteboardTitle : '');
    boardsByMessage.set(messageId, boards);
}

/** The buttons under a finished answer: open the whiteboards it changed, pin it to a whiteboard. */
function appendAnswerActions(messageId, answerText, afterElement) {
    const boards = boardsByMessage.get(messageId);
    const offerPin = hostFeatures.pinToWhiteboard && Boolean(answerText && answerText.trim());
    const offerOpen = hostFeatures.openWhiteboard && Boolean(boards && boards.size > 0);
    if (!offerPin && !offerOpen) return;
    const row = document.createElement('div');
    row.className = 'answer-actions';
    if (offerOpen) {
        boards.forEach((title, whiteboardId) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'answer-action';
            button.textContent = title ? `Open "${title}"` : 'Open the whiteboard';
            button.addEventListener('click', () => {
                window.parent.postMessage({ type: 'STRANDS_OPEN_WHITEBOARD', whiteboardId }, PARENT_ORIGIN);
            });
            row.appendChild(button);
        });
    }
    if (offerPin) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'answer-action';
        button.textContent = 'Pin to whiteboard';
        button.title = "Add this answer to one of the meeting's whiteboards";
        button.addEventListener('click', () => {
            window.parent.postMessage(
                {
                    type: 'STRANDS_PIN_TO_WHITEBOARD',
                    text: answerText,
                },
                PARENT_ORIGIN,
            );
        });
        row.appendChild(button);
    }
    if (afterElement && afterElement.parentNode === chatMessages) {
        afterElement.after(row);
    } else {
        chatMessages.appendChild(row);
    }
}

// The full-size picture is shown in this frame: the page frames it with
// a sandbox that allows no popups, so window.open would be dropped.
function openKeyframeOverlay(url, alt, frameId) {
    const overlay = document.createElement('div');
    overlay.className = 'lma-keyframe-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-label', alt || 'Shared screen');
    const big = document.createElement('img');
    big.src = url;
    big.alt = alt || '';
    overlay.appendChild(big);
    const close = () => {
        overlay.remove();
        document.removeEventListener('keydown', onKey);
    };
    const onKey = (event) => {
        if (event.key === 'Escape') close();
    };
    if (hostFeatures.showInTranscript && frameId) {
        overlay.classList.add('with-actions');
        const show = document.createElement('button');
        show.type = 'button';
        show.textContent = 'Show in transcript';
        show.addEventListener('click', (event) => {
            event.stopPropagation();
            close();
            window.parent.postMessage({ type: 'STRANDS_SHOW_IN_TRANSCRIPT', frameId }, PARENT_ORIGIN);
        });
        overlay.appendChild(show);
    }
    overlay.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
}

function showKeyframe(img, link) {
    const url = link && (link.image || link.thumbnail);
    if (!url || !url.startsWith('https://')) return;
    img.src = url;
    img.title = 'Show full size';
    // An expired or failed link is fetched again, once.
    img.onerror = () => {
        if (img.dataset.lmaRetried) return;
        img.dataset.lmaRetried = '1';
        keyframeLinks.delete(img.dataset.lmaKeyframe);
        img.removeAttribute('src');
        resolveKeyframes(img.parentElement);
    };
    img.onclick = () => {
        const current = freshLink(img.dataset.lmaKeyframe);
        if (current) openKeyframeOverlay(current.image || current.thumbnail, img.alt, img.dataset.lmaKeyframe);
        else resolveKeyframes(img.parentElement);
    };
}

function resolveKeyframes(root) {
    if (!root) return;
    const missing = new Set();
    root.querySelectorAll('img[data-lma-keyframe]').forEach((img) => {
        const link = freshLink(img.dataset.lmaKeyframe);
        if (link) showKeyframe(img, link);
        else missing.add(img.dataset.lmaKeyframe);
    });
    if (!missing.size) return;
    const requestId = `kf-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    // A page that does not answer leaves the placeholders with their
    // text, and no listener behind.
    const giveUp = setTimeout(() => window.removeEventListener('message', onFrames), 30000);
    const onFrames = (event) => {
        if (!isFromParent(event) || event.data?.type !== 'STRANDS_KEYFRAMES' || event.data.requestId !== requestId) return;
        clearTimeout(giveUp);
        window.removeEventListener('message', onFrames);
        const fetchedAt = Date.now();
        Object.entries(event.data.frames || {}).forEach(([frameId, link]) =>
            keyframeLinks.set(frameId, { ...link, fetchedAt }));
        root.querySelectorAll('img[data-lma-keyframe]').forEach((img) => {
            const link = freshLink(img.dataset.lmaKeyframe);
            if (link) showKeyframe(img, link);
        });
    };
    window.addEventListener('message', onFrames);
    window.parent.postMessage(
        { type: 'STRANDS_RESOLVE_KEYFRAMES', requestId, callId, frameIds: [...missing].slice(0, 10) },
        PARENT_ORIGIN,
    );
}

// Messages from the page that hosts this frame.
window.addEventListener('message', (event) => {
    if (!isFromParent(event)) return;
    if (event.data?.type === 'STRANDS_RELOAD_BUTTONS') {
        // Reload button configuration when admin saves changes
        loadButtonConfiguration();
    } else if (event.data?.type === 'STRANDS_FOCUS_INPUT') {
        // The meeting page's assistant panel was opened.
        messageInput.focus();
    } else if (event.data?.type === 'STRANDS_HOST_FEATURES') {
        hostFeatures.showInTranscript = event.data.showInTranscript === true;
        hostFeatures.pinToWhiteboard = event.data.pinToWhiteboard === true;
        hostFeatures.openWhiteboard = event.data.openWhiteboard === true;
    } else if (event.data?.type === 'STRANDS_ASK_WITH_CONTEXT') {
        const { label, text, question } = event.data;
        if (typeof label !== 'string' || typeof text !== 'string' || !text.trim()) return;
        setAskContext({
            label: label.slice(0, 200),
            text: text.slice(0, MAX_CONTEXT_CHARS),
            question: typeof question === 'string' ? question.slice(0, 500) : '',
        });
    }
});

// Escape in this frame closes the meeting page's assistant panel. Keys
// pressed in a frame do not reach the page that hosts it, so it is told.
// A picture open full size takes Escape for itself (openKeyframeOverlay).
document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (document.querySelector('.lma-keyframe-overlay')) return;
    window.parent.postMessage({ type: 'STRANDS_ESCAPE' }, PARENT_ORIGIN);
});

// Thinking process functions
function createThinkingContainer(messageId) {
    const container = document.createElement('div');
    container.className = 'thinking-container';
    container.id = `thinking-${messageId}`;

    const header = document.createElement('div');
    header.className = 'thinking-header';

    // Built with DOM nodes rather than an HTML string because messageId
    // comes from the backend response.
    const currentStep = document.createElement('div');
    currentStep.className = 'thinking-current-step';

    const spinnerWrapper = document.createElement('div');
    spinnerWrapper.style.width = '12px';
    const spinner = document.createElement('span');
    spinner.className = 'thinking-spinner';
    spinnerWrapper.appendChild(spinner);

    const currentStepLabel = document.createElement('span');
    currentStepLabel.id = `current-step-text-${messageId}`;
    currentStepLabel.textContent = 'Analyzing request...';

    currentStep.appendChild(spinnerWrapper);
    currentStep.appendChild(document.createTextNode(' '));
    currentStep.appendChild(currentStepLabel);

    const toggle = document.createElement('div');
    toggle.className = 'thinking-toggle';
    toggle.textContent = '▼';

    header.appendChild(currentStep);
    header.appendChild(toggle);

    const details = document.createElement('div');
    details.className = 'thinking-details';
    details.id = `thinking-details-${messageId}`;

    container.appendChild(header);
    container.appendChild(details);

    // Toggle expand/collapse
    header.addEventListener('click', () => {
        details.classList.toggle('expanded');
        toggle.textContent = details.classList.contains('expanded')
            ? '▲'
            : '▼';
    });

    chatMessages.appendChild(container);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return container;
}

function addThinkingStep(messageId, step) {
    if (!thinkingSteps.has(messageId)) {
        thinkingSteps.set(messageId, []);
    }

    const steps = thinkingSteps.get(messageId);
    steps.push(step);

    // Update current step display
    updateCurrentStepDisplay(messageId, step);

    // Add to detailed view
    addStepToDetailedView(messageId, step);
}

function updateCurrentStepDisplay(messageId, step) {
    const currentStepText = document.getElementById(`current-step-text-${messageId}`);
    if (!currentStepText) {
        console.warn('[STRANDS] current-step-text element not found for:', messageId);
        return;
    }

    let displayText = '';
    let icon = '';

    switch(step.Type) {
        case 'reasoning':
            icon = '🧠';
            displayText = step.Content || 'Thinking...';
            if (displayText.length > 60) {
                displayText = displayText.substring(0, 60) + '...';
            }
            break;
        case 'tool_use':
            icon = '🔧';
            displayText = `Using ${formatToolName(step.ToolName)}...`;
            break;
        case 'tool_result':
            icon = step.Success ? '✅' : '❌';
            displayText = step.Success
                ? `Completed ${formatToolName(step.ToolName)}`
                : `Error in ${formatToolName(step.ToolName)}`;
            break;
        case 'status':
            icon = '⚙️';
            displayText = step.Content || 'Processing...';
            break;
    }

    // step.Content is model output and the label needs no markup, so it
    // is written as text.
    currentStepText.textContent = `${icon} ${displayText}`;
}

function addStepToDetailedView(messageId, step) {
    const details = document.getElementById(`thinking-details-${messageId}`);
    if (!details) {
        console.warn('[STRANDS] thinking-details element not found for:', messageId);
        return;
    }

    const stepDiv = document.createElement('div');
    let stepClass = 'thinking-step ' + step.Type.replace('_', '-');
    if (step.Type === 'tool_result' && !step.Success) {
        stepClass += ' error';
    }
    stepDiv.className = stepClass;

    const header = document.createElement('div');
    header.className = 'thinking-step-header';

    let icon = '';
    let title = '';

    switch(step.Type) {
        case 'reasoning':
            icon = '🧠';
            title = 'Reasoning';
            break;
        case 'tool_use':
            icon = '🔧';
            title = `Tool: ${formatToolName(step.ToolName)}`;
            break;
        case 'tool_result':
            icon = step.Success ? '✅' : '❌';
            title = step.Success ? 'Result' : 'Error';
            break;
        case 'status':
            icon = '⚙️';
            title = 'Status';
            break;
    }

    // `title` embeds step.ToolName from the agent, so the two spans are
    // created as nodes and filled with text.
    const iconSpan = document.createElement('span');
    iconSpan.textContent = icon;
    const titleSpan = document.createElement('span');
    titleSpan.textContent = title;
    header.appendChild(iconSpan);
    header.appendChild(titleSpan);
    stepDiv.appendChild(header);

    if (step.Content) {
        const content = document.createElement('div');
        content.className = 'thinking-step-content';
        content.textContent = step.Content;
        stepDiv.appendChild(content);
    }

    if (step.ToolInput) {
        const params = document.createElement('div');
        params.className = 'tool-params';
        try {
            const parsed = JSON.parse(step.ToolInput);
            params.textContent = `Parameters: ${JSON.stringify(parsed, null, 2)}`;
        } catch (e) {
            params.textContent = `Parameters: ${step.ToolInput}`;
        }
        stepDiv.appendChild(params);
    }

    if (step.ToolResult) {
        const result = document.createElement('div');
        result.className = 'thinking-step-content';
        result.textContent = truncateText(step.ToolResult, 200);
        stepDiv.appendChild(result);
    }

    const timestamp = document.createElement('div');
    timestamp.className = 'thinking-step-timestamp';
    timestamp.textContent = formatTimestamp(step.Timestamp);
    stepDiv.appendChild(timestamp);

    details.appendChild(stepDiv);

    if (details.classList.contains('expanded')) {
        details.scrollTop = details.scrollHeight;
    }
}

function formatToolName(toolName) {
    if (!toolName) return 'tool';
    return toolName.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
}

function formatTimestamp(timestamp) {
    if (!timestamp) return '';

    try {
        // Handle ISO 8601 format timestamps
        const date = new Date(timestamp);

        // Check if date is valid
        if (isNaN(date.getTime())) {
            return '';
        }

        // Format as HH:MM:SS
        return date.toLocaleTimeString('en-US', {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hour12: false
        });
    } catch (e) {
        console.error('Error formatting timestamp:', e);
        return '';
    }
}

function truncateText(text, maxLength) {
    if (!text) return '';
    if (text.length <= maxLength) return text;
    return text.substring(0, maxLength) + '...';
}

function finalizeThinkingContainer(messageId) {
    const currentStepText = document.getElementById(`current-step-text-${messageId}`);
    if (currentStepText) {
        currentStepText.textContent = '✓ Complete';
    }

    const container = document.getElementById(`thinking-${messageId}`);
    if (container) {
        const spinner = container.querySelector('.thinking-spinner');
        if (spinner) {
            spinner.remove();
        }
    }
}

function addMessage(text, type = 'assistant') {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${type}`;

    // Use markdown rendering for assistant messages, plain text for user/error
    const rendered = type === 'assistant' ? renderAssistantMarkdown(text) : null;
    if (rendered !== null) {
        messageDiv.innerHTML = rendered;
        resolveKeyframes(messageDiv);
    } else {
        messageDiv.textContent = text;
    }

    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    // Store in conversation history (skip error messages)
    if (type !== 'error') {
        conversationHistory.push({
            role: type === 'user' ? 'user' : 'assistant',
            content: text,
            timestamp: new Date().toISOString()
        });

        // Keep only last N messages
        if (conversationHistory.length > MAX_HISTORY_MESSAGES) {
            conversationHistory.shift(); // Remove oldest
        }
    }
}

/** A question asked about something on the page: the question, and what it is about. */
function addContextQuestion(question, context, message) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message user';
    messageDiv.textContent = question;
    const about = document.createElement('span');
    about.className = 'message-context';
    about.textContent = `About: ${context.label}`;
    messageDiv.appendChild(about);
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    conversationHistory.push({ role: 'user', content: message, timestamp: new Date().toISOString() });
    if (conversationHistory.length > MAX_HISTORY_MESSAGES) {
        conversationHistory.shift();
    }
}

function addLoadingMessage() {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message assistant loading';
    messageDiv.textContent = 'Thinking...';
    messageDiv.id = 'loadingMessage';
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return messageDiv;
}

function removeLoadingMessage() {
    const loadingMsg = document.getElementById('loadingMessage');
    if (loadingMsg) {
        loadingMsg.remove();
    }
}

async function sendMessage() {
    const typed = messageInput.value.trim();
    const context = pendingContext;
    // With something to ask about, sending nothing asks its question.
    const question = typed || (context && context.question) || '';
    if (!question) return;
    const message = context ? withContext(question, context) : question;

    // Remove inline suggestion buttons while processing
    const previousInline = chatMessages.querySelectorAll('.inline-suggestions');
    previousInline.forEach(el => el.remove());

    if (context) {
        addContextQuestion(question, context, message);
        setAskContext(null);
    } else {
        addMessage(message, 'user');
    }
    messageInput.value = '';
    sendButton.disabled = true;

    try {
        const response = await callSendChatMessageAPI(message, callId);

        if (response?.data?.sendChatMessage) {
            const messageId = response.data.sendChatMessage.MessageId;

            // Create thinking container (replaces old loading message)
            createThinkingContainer(messageId);

            // Subscribe to token stream (thinking steps come through here too)
            subscribeToTokenStream(messageId);

            // Fallback for non-streaming responses
            if (response.data.sendChatMessage.Response) {
                finalizeThinkingContainer(messageId);
                addMessage(response.data.sendChatMessage.Response, 'assistant');
                appendAnswerActions(messageId, response.data.sendChatMessage.Response, null);
                // Append shortcut buttons after fallback response
                appendInlineSuggestionButtons();
            }
        } else {
            addMessage('Unexpected response format', 'error');
        }
    } catch (error) {
        addMessage(`Error: ${error.message}`, 'error');
    }

    sendButton.disabled = false;
}

async function callSendChatMessageAPI(message, callId) {
    return new Promise((resolve, reject) => {
        const messageId = `strands-chat-${Date.now()}`;

        const messageHandler = (event) => {
            if (!isFromParent(event)) return;
            if (event.data?.type === 'STRANDS_CHAT_RESPONSE' && event.data.messageId === messageId) {
                window.removeEventListener('message', messageHandler);
                event.data.success ? resolve(event.data.result) : reject(new Error(event.data.error));
            }
        };

        window.addEventListener('message', messageHandler);

        if (window.parent && window.parent !== window) {
            window.parent.postMessage({
                type: 'STRANDS_CHAT_REQUEST',
                messageId,
                callId,
                message,
                conversationHistory: conversationHistory
            }, PARENT_ORIGIN);

            setTimeout(() => {
                window.removeEventListener('message', messageHandler);
                reject(new Error('Request timeout'));
            }, 30000);
        } else {
            reject(new Error('No parent window available'));
        }
    });
}

// Assembles a streamed answer from its chunks. AppSync does not
// guarantee that a subscriber receives the chunks in the order they were
// published, so each is placed by its Sequence (the producer numbers an
// answer's chunks 0, 1, 2, ...) and a chunk received twice is kept once.
// A chunk without a Sequence is placed after every numbered one, in the
// order received. add() returns false for a chunk already held.
function createAnswerAssembler() {
    const numbered = new Map();
    const unnumbered = [];
    return {
        add(token) {
            const sequence = token.Sequence;
            if (Number.isInteger(sequence)) {
                if (numbered.has(sequence)) return false;
                numbered.set(sequence, token.Token);
            } else {
                unnumbered.push(token.Token);
            }
            return true;
        },
        text() {
            const ordered = [...numbered.keys()].sort((a, b) => a - b).map((key) => numbered.get(key));
            return ordered.concat(unnumbered).join('');
        },
    };
}

function subscribeToTokenStream(messageId) {
    const streamingMessageDiv = document.createElement('div');
    streamingMessageDiv.className = 'message assistant';
    streamingMessageDiv.id = `streaming-message-${messageId}`;
    streamingMessageDiv.textContent = '';
    chatMessages.appendChild(streamingMessageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    // The answer's chunks, put back in the order they were published.
    const answer = createAnswerAssembler();
    let accumulatedText = '';

    window.parent.postMessage({
        type: 'STRANDS_SETUP_TOKEN_SUBSCRIPTION',
        callId,
        messageId
    }, PARENT_ORIGIN);

    const tokenHandler = (event) => {
        if (!isFromParent(event)) return;
        if (event.data?.type === 'STRANDS_TOKEN_MESSAGE') {
            const token = event.data.token;

            if (token.MessageId === messageId) {
                // Check if this token contains a thinking step
                if (token.ThinkingStep) {
                    addThinkingStep(messageId, token.ThinkingStep);
                    noteWhiteboardChange(messageId, token.ThinkingStep);
                }

                if (token.IsComplete) {
                    finalizeThinkingContainer(messageId);

                    // The completion marker carries the whole answer as
                    // generated; it replaces the chunks received. A marker
                    // without it keeps the text assembled from the chunks.
                    if (token.Token) {
                        accumulatedText = token.Token;
                    }

                    // Final render with markdown
                    if (accumulatedText) {
                        const finalHtml = renderAssistantMarkdown(accumulatedText);
                        if (finalHtml !== null) {
                            streamingMessageDiv.innerHTML = finalHtml;
                            resolveKeyframes(streamingMessageDiv);
                        } else {
                            streamingMessageDiv.textContent = accumulatedText;
                        }
                    }

                    // Add completed response to conversation history
                    if (accumulatedText) {
                        conversationHistory.push({
                            role: 'assistant',
                            content: accumulatedText,
                            timestamp: new Date().toISOString()
                        });

                        // Keep only last N messages
                        if (conversationHistory.length > MAX_HISTORY_MESSAGES) {
                            conversationHistory.shift(); // Remove oldest
                        }
                    }

                    appendAnswerActions(messageId, accumulatedText, streamingMessageDiv);

                    // Append shortcut buttons after response completes
                    appendInlineSuggestionButtons();

                    window.removeEventListener('message', tokenHandler);
                } else if (token.Token && !token.ThinkingStep) {
                    // Only accumulate if there's actual token text (not just thinking steps)
                    if (!answer.add(token)) return;
                    accumulatedText = answer.text();

                    // Render markdown incrementally during streaming
                    const partialHtml = renderAssistantMarkdown(accumulatedText);
                    if (partialHtml !== null) {
                        streamingMessageDiv.innerHTML = partialHtml;
                        // Frames already resolved keep their picture while the answer streams.
                        streamingMessageDiv.querySelectorAll('img[data-lma-keyframe]').forEach((img) => {
                            const link = freshLink(img.dataset.lmaKeyframe);
                            if (link) showKeyframe(img, link);
                        });
                    } else {
                        streamingMessageDiv.textContent = accumulatedText;
                    }

                    chatMessages.scrollTop = chatMessages.scrollHeight;
                }
            }
        }
    };

    window.addEventListener('message', tokenHandler);
}

// Append shortcut buttons inline in the chat after a response completes
function appendInlineSuggestionButtons() {
    if (!buttonConfig || Object.keys(buttonConfig).length === 0) return;

    // Remove any previous inline suggestion buttons (keep only the original in welcome message)
    const previousInline = chatMessages.querySelectorAll('.inline-suggestions');
    previousInline.forEach(el => el.remove());

    // Create a new inline suggestions container
    const container = document.createElement('div');
    container.className = 'suggestions-container inline-suggestions';

    // Sort buttons by sequence number
    const sortedButtons = Object.entries(buttonConfig)
        // A button is stored under an "N#LABEL" key, which is what the sort
        // and the label below both already assume. Testing for it here too
        // keeps this renderer from depending on what the parent sent: any
        // other attribute of the stored record would otherwise draw a button
        // labelled with its own key.
        .filter(([key]) => /^\d+#/.test(key))
        .sort((a, b) => {
            const getSequence = (key) => {
                const match = key.match(/^(\d+)#/);
                return match ? parseInt(match[1]) : 999;
            };
            return getSequence(a[0]) - getSequence(b[0]);
        });

    const visibleCount = 4;
    let hasHiddenButtons = false;

    sortedButtons.forEach(([key, prompt], index) => {
        if (!prompt || prompt.trim() === '' || prompt.toUpperCase() === 'NONE') return;

        const label = key.replace(/^\d+#/, '');
        const button = document.createElement('button');
        button.className = 'suggestion-button';
        button.setAttribute('data-prompt', prompt);
        button.textContent = label;

        if (index >= visibleCount) {
            button.classList.add('extra-suggestion-inline');
            button.style.display = 'none';
            hasHiddenButtons = true;
        }

        button.addEventListener('click', () => {
            messageInput.value = prompt;
            messageInput.focus();
            sendMessage();
        });

        container.appendChild(button);
    });

    if (hasHiddenButtons) {
        const moreButton = document.createElement('button');
        moreButton.className = 'more-button';
        moreButton.textContent = 'MORE';
        moreButton.addEventListener('click', () => {
            const extraButtons = container.querySelectorAll('.extra-suggestion-inline');
            extraButtons.forEach(btn => {
                btn.style.display = 'inline-block';
            });
            moreButton.style.display = 'none';
        });
        container.appendChild(moreButton);
    }

    // Only append if we actually added buttons
    if (container.children.length > 0) {
        chatMessages.appendChild(container);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }
}

// Load button configuration from DynamoDB
async function loadButtonConfiguration() {
    try {
        const config = await fetchButtonConfiguration();
        if (config) {
            buttonConfig = config;
            renderSuggestionButtons(config);
        } else {
            // Fallback to default hardcoded buttons if config fetch fails
            renderDefaultButtons();
        }
    } catch (error) {
        console.error('Error loading button configuration:', error);
        renderDefaultButtons();
    }
}

async function fetchButtonConfiguration() {
    return new Promise((resolve, reject) => {
        const requestId = `button-config-${Date.now()}`;

        const configHandler = (event) => {
            if (!isFromParent(event)) return;
            if (event.data?.type === 'STRANDS_BUTTON_CONFIG_RESPONSE' && event.data.requestId === requestId) {
                window.removeEventListener('message', configHandler);
                event.data.success ? resolve(event.data.config) : reject(new Error(event.data.error));
            }
        };

        window.addEventListener('message', configHandler);

        if (window.parent && window.parent !== window) {
            window.parent.postMessage({
                type: 'STRANDS_BUTTON_CONFIG_REQUEST',
                requestId
            }, PARENT_ORIGIN);

            setTimeout(() => {
                window.removeEventListener('message', configHandler);
                reject(new Error('Button config request timeout'));
            }, 5000);
        } else {
            reject(new Error('No parent window available'));
        }
    });
}

function renderSuggestionButtons(config) {
    const container = document.getElementById('suggestionsContainer');
    container.replaceChildren();

    if (!config || Object.keys(config).length === 0) {
        renderDefaultButtons();
        return;
    }

    // Sort buttons by sequence number (N# prefix)
    const sortedButtons = Object.entries(config)
        // As above: a button is an "N#LABEL" key, so this renderer states
        // that for itself rather than trusting what it was handed.
        .filter(([key]) => /^\d+#/.test(key))
        .sort((a, b) => {
            const getSequence = (key) => {
                const match = key.match(/^(\d+)#/);
                return match ? parseInt(match[1]) : 999;
            };
            return getSequence(a[0]) - getSequence(b[0]);
        });

    // Show first 4 buttons, hide rest initially
    const visibleCount = 4;
    let hasHiddenButtons = false;

    sortedButtons.forEach(([key, prompt], index) => {
        // Skip if prompt is empty or "NONE"
        if (!prompt || prompt.trim() === '' || prompt.toUpperCase() === 'NONE') {
            return;
        }

        // Extract label from key (remove N# prefix)
        const label = key.replace(/^\d+#/, '');

        const button = document.createElement('button');
        button.className = 'suggestion-button';
        button.setAttribute('data-prompt', prompt);
        button.textContent = label;

        if (index >= visibleCount) {
            button.classList.add('extra-suggestion');
            button.style.display = 'none';
            hasHiddenButtons = true;
        }

        button.addEventListener('click', () => {
            messageInput.value = prompt;
            messageInput.focus();
            sendMessage();
        });

        container.appendChild(button);
    });

    // Add MORE button if there are hidden buttons
    if (hasHiddenButtons) {
        const moreButton = document.createElement('button');
        moreButton.className = 'more-button';
        moreButton.id = 'moreButton';
        moreButton.textContent = 'MORE';
        moreButton.addEventListener('click', () => {
            const extraButtons = document.querySelectorAll('.extra-suggestion');
            extraButtons.forEach(btn => {
                btn.style.display = 'inline-block';
            });
            moreButton.style.display = 'none';
        });
        container.appendChild(moreButton);
    }
}

function renderDefaultButtons() {
    // Fallback to hardcoded default buttons (commented out, no defaults for now)
    const defaultButtons = [
        // { label: 'SUMMARIZE', prompt: 'Summarize this meeting' },
        // { label: 'ACTIONS', prompt: 'What were the action items?' },
        // { label: 'TOPICS', prompt: 'What topics were discussed?' },
        // { label: 'ASK ASSISTANT!', prompt: 'Please respond to the last question or instruction in this meeting.' },
        // { label: 'FACT CHECK', prompt: 'Can you fact-check the recent statements made in this meeting?', hidden: true },
        // { label: 'DISCUSSED BEFORE?', prompt: 'Based on what we\'re discussing in this meeting, have we talked about this topic in previous meetings?', hidden: true },
        // { label: 'FIND DOCS', prompt: 'Based on the current meeting discussion, are there any relevant company documents or policies?', hidden: true }
    ];

    const container = document.getElementById('suggestionsContainer');
    container.replaceChildren();

    let hasHiddenButtons = false;

    defaultButtons.forEach(({ label, prompt, hidden }) => {
        const button = document.createElement('button');
        button.className = 'suggestion-button';
        button.setAttribute('data-prompt', prompt);
        button.textContent = label;

        if (hidden) {
            button.classList.add('extra-suggestion');
            button.style.display = 'none';
            hasHiddenButtons = true;
        }

        button.addEventListener('click', () => {
            messageInput.value = prompt;
            messageInput.focus();
            sendMessage();
        });

        container.appendChild(button);
    });

    if (hasHiddenButtons) {
        const moreButton = document.createElement('button');
        moreButton.className = 'more-button';
        moreButton.id = 'moreButton';
        moreButton.textContent = 'MORE';
        moreButton.addEventListener('click', () => {
            const extraButtons = document.querySelectorAll('.extra-suggestion');
            extraButtons.forEach(btn => {
                btn.style.display = 'inline-block';
            });
            moreButton.style.display = 'none';
        });
        container.appendChild(moreButton);
    }
}

sendButton.addEventListener('click', sendMessage);
messageInput.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendMessage();
});

// Last, so every declaration it reaches (PARENT_ORIGIN, isFromParent and
// the render helpers) is initialised before the request is posted.
loadButtonConfiguration();
