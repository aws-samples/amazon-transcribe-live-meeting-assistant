# Frontend UI Development — LMA

Conventions for the React UI at `lma-ai-stack/source/ui/`. Read alongside
`code-review.md` for the pre-commit checklist.

## Stack

- **Framework**: React 18.3.1
- **Language**: JavaScript (`.js` / `.jsx`) — **not** TypeScript. The repo
  uses ESLint with airbnb + prettier; there is no `tsconfig.json` for the UI
- **Bundler**: **Vite** (`@vitejs/plugin-react`). Scripts: `npm start` / `npm run dev`
  → `vite`; `npm run build` → `vite build`; `npm test` → `vitest`
- **UI library**: Cloudscape Design System v3
  (`@cloudscape-design/components`, `@cloudscape-design/global-styles`,
  `@cloudscape-design/collection-hooks`)
- **Auth**: AWS Amplify v6 + Cognito (`aws-amplify`, `@aws-amplify/ui-react`)
- **API**: AWS AppSync GraphQL (queries/mutations/subscriptions in
  `src/graphql/`)
- **Router**: react-router-dom v6 (HashRouter)
- **State**: React Context + hooks. **No Redux.**
- **Node**: `>=20.0.0`, npm `>=10.0.0`
- **Module type**: ESM (`"type": "module"` in `package.json`)
- **Test**: vitest + `@testing-library/react`

## Directory Structure

```
lma-ai-stack/source/ui/src/
├── App.jsx                # Root: ThemeProvider > Authenticator > AppContent
├── index.jsx              # Entry point
├── aws-exports.js         # Amplify config (rewritten at deploy time)
├── components/            # 22 feature directories + common/
│   ├── common/
│   ├── call-list/
│   ├── call-details/
│   ├── call-panel/
│   ├── call-analytics-layout/
│   ├── stream-audio-layout/
│   ├── upload-audio-layout/
│   ├── meetings-query-layout/
│   ├── virtual-participant-layout/
│   ├── mcp-servers-page/
│   ├── nova-sonic-config/
│   ├── transcript-summary-config/
│   └── ...
├── contexts/              # React Context providers
│   ├── app.js             # AppContext (auth, awsConfig, navigation)
│   ├── calls.js           # CallsContext (call list, filters)
│   └── settings.js        # SettingsContext
├── hooks/                 # Custom hooks (kebab-case for new ones)
│   ├── use-aws-config.js
│   ├── use-current-session-creds.js
│   ├── use-mcp-config.js
│   ├── use-calls-graphql-api.js
│   └── ...
├── graphql/
│   ├── mutations.js       # AUTO-GENERATED — starts with /* eslint-disable */
│   └── queries/           # Hand-maintained query files (one per operation)
│       ├── getCall.js
│       ├── listCallsDateRange.js
│       ├── onAddTranscriptSegment.js
│       └── ...
├── routes/                # AuthRoutes / UnauthRoutes
└── setupTests.js
```

## Component Pattern (MUST follow)

Arrow function components are **enforced** by ESLint
(`react/function-component-definition: namedComponents = arrow-function`):

```jsx
import React, { useState, useEffect, useMemo } from 'react';
import {
  Table,
  Pagination,
  TextFilter,
  Box,
  SpaceBetween,
} from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { ConsoleLogger } from 'aws-amplify/utils';

const logger = new ConsoleLogger('MyComponent');

const MyComponent = () => {
  // 1. Hooks (context, state, refs, navigation)
  const [data, setData] = useState([]);

  // 2. Effects
  useEffect(() => {
    /* ... */
  }, []);

  // 3. Memos / derived state
  const filtered = useMemo(() => data.filter(/* ... */), [data]);

  // 4. Handlers
  const handleClick = () => {
    logger.debug('clicked');
  };

  // 5. Render
  return (
    <SpaceBetween size="l">
      <Table items={filtered} />
    </SpaceBetween>
  );
};

export default MyComponent;
```

Component files use **PascalCase** within **kebab-case** directories — e.g.
`components/call-list/CallList.jsx`, `components/call-list/CallListSplitPanel.jsx`,
and an `index.js` that re-exports the main component.

## Context Pattern

```js
// src/contexts/app.js
import { createContext, useContext } from 'react';

export const AppContext = createContext(null);

const useAppContext = () => {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppContext must be used within AppContext.Provider');
  return ctx;
};

export default useAppContext;
```

## Auth Pattern (Amplify v6)

```jsx
import { useAuthenticator } from '@aws-amplify/ui-react';
import { fetchAuthSession } from 'aws-amplify/auth';

const { user, signOut } = useAuthenticator();
const session = await fetchAuthSession();
const credentials = session.credentials;
```

## Logging Pattern

Use Amplify's `ConsoleLogger` per component. The eslint config has
`no-console: off`, so plain `console.log` won't fail the build, but new code
should use `ConsoleLogger` for consistency with the rest of the UI.

```jsx
import { ConsoleLogger } from 'aws-amplify/utils';
const logger = new ConsoleLogger('ComponentName');
logger.debug('message');
logger.error('error', error);
```

## GraphQL / AppSync

- **Schema** lives at `lma-ai-stack/source/appsync/schema.graphql`
  (server-side). Resolvers under `lma-ai-stack/source/appsync/`.
- **Client mutations** are auto-generated into
  `lma-ai-stack/source/ui/src/graphql/mutations.js` (file starts with
  `/* eslint-disable */` and the comment "this is an auto generated
  file"). Do **not** hand-edit.
- **Client queries / subscriptions** live as one-file-per-operation under
  `src/graphql/queries/` and are hand-maintained. They include the LMA
  copyright header and may be edited as needed.
- If the AppSync `schema.graphql` changes, update server-side resolvers and
  the client-side query files in the **same** PR.

## Lint & Format Config

`lma-ai-stack/source/ui/.eslintrc.json`:
- Extends `airbnb`, `plugin:react/recommended`, `plugin:prettier/recommended`
- `max-len`: 120 (ignoring URLs and template literals)
- `linebreak-style`: unix
- `react/jsx-filename-extension`: `.js`, `.jsx`
- `react/function-component-definition`: arrow-function (errored)
- `no-console`: off
- `no-alert`: off

`lma-ai-stack/source/ui/.prettierrc`:
```json
{
  "printWidth": 120,
  "singleQuote": true,
  "trailingComma": "all"
}
```

## Hook File Naming

- **NEW** hooks: kebab-case (`use-my-hook.js`). Existing hooks already
  follow this pattern: `use-aws-config.js`, `use-calls-graphql-api.js`,
  `use-current-session-creds.js`, etc.
- Don't rename existing hooks even if you find one that doesn't match.

## CSS

- Cloudscape global styles imported in `index.jsx`:
  `import '@cloudscape-design/global-styles/index.css'`
- Component-level styles via Cloudscape props or
  `import './ComponentName.css'`

## Copyright Header

Hand-maintained source files (`.js`, `.jsx`) start with:

```js
/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
```

The auto-generated `mutations.js` is the exception.

## Commands

```bash
cd lma-ai-stack/source/ui
npm install
npm start                 # Vite dev server (default port 3000 / 5173)
npm run build             # Production build
npm test                  # vitest

# Lint via the AI stack Makefile
cd lma-ai-stack
make lint-eslint
make lint-prettier
make lint-javascript      # eslint + prettier
```

## Quick Reference — UI Red Flags

- `function MyComponent()` — should be arrow function (lint error)
- New hook named `useMyThing.js` — should be `use-my-thing.js`
- Hand-edit to `graphql/mutations.js` — should regenerate
- Missing copyright header on a new `.js`/`.jsx` file
- New dependency on Material UI / Ant Design / Bootstrap — use Cloudscape
- Direct `fetch(...)` to AppSync — use the existing GraphQL hook helpers in
  `src/hooks/use-*-graphql-api.js`
- `dangerouslySetInnerHTML` without `DOMPurify` sanitization
