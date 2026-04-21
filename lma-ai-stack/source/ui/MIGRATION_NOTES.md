# LMA UI Modernization — Migration Notes

The LMA web UI was moved from Create React App (CRA) to **Vite**, with simultaneous upgrades of Cloudscape, AWS Amplify, and React Router. This document summarises what changed and how to work with the new stack.

## Stack summary

| Area                | Before (CRA)                          | After                                      |
| ------------------- | ------------------------------------- | ------------------------------------------ |
| Build tool          | `react-scripts` 4.x (Webpack 4)       | **Vite 7** + `@vitejs/plugin-react` 5      |
| React               | 17                                    | **18**                                     |
| Cloudscape          | `@awsui/components-react` 3.0.x       | **`@cloudscape-design/components` 3.0.x**  |
| Amplify JS          | v4 (`aws-amplify`, `@aws-amplify/ui`) | **v6** (`aws-amplify`, `@aws-amplify/ui-react`) |
| Router              | react-router-dom v5                   | **v6**                                     |
| Env-var prefix      | `REACT_APP_*`                          | **`VITE_*`** (exposed via `import.meta.env`) |
| Output directory    | `build/`                              | `build/` (preserved; see `vite.config.js`) |

## Running locally

Use the top-level `make ui-start` target — it reads the `LocalUITestingEnv`
output from the deployed CloudFormation stack, writes the UI `.env` file, and
starts the Vite dev server:

```sh
make ui-start STACK_NAME=<your-lma-stack-name>
```

Vite reads `.env` / `.env.local` / `.env.production` automatically; the
previous CRA `.env` file (with `REACT_APP_*` keys) is no longer honoured — the
`make ui-start` target now emits `VITE_*` keys.

## Building

```sh
npm run lint     # eslint (prettier + import rules) — zero errors today
npm run build    # vite build → build/
```

## What was changed in code

1. **Vite scaffold**
   - New files: `vite.config.js`, `index.html`.
   - Renamed `src/index.js` → `src/index.jsx`; entry point is `/src/index.jsx`.
   - Added an `esbuild.loader: 'jsx'` rule so legacy `.js` files containing JSX still parse.
   - Added `optimizeDeps.esbuildOptions.loader['.js'] = 'jsx'` for dev-server prebundling.

2. **Cloudscape rename** — `@awsui/*` → `@cloudscape-design/*` across the tree.

3. **Amplify v6**
   - `Logger` → `ConsoleLogger` from `aws-amplify/utils`.
   - `Auth.*` → `signIn/signOut/fetchAuthSession` from `aws-amplify/auth`.
   - `API.graphql(graphqlOperation(Q,V))` → `client.graphql({ query:Q, variables:V })` using `generateClient()` from `aws-amplify/api`.
   - `withAuthenticator`/`AmplifySignIn` → `<Authenticator>` + `useAuthenticator` from `@aws-amplify/ui-react` (v6 UI).
   - `Amplify.Logger.LOG_LEVEL` → `ConsoleLogger.LOG_LEVEL` with a `DEV`-aware default.

4. **React Router v6**
   - `Switch` → `Routes`, `<Redirect>` → `<Navigate>`.
   - `useHistory` → `useNavigate` (`history.goBack()` → `navigate(-1)`).
   - `useRouteMatch().path` removed; parent routes in `AuthRoutes.jsx` use the
     `/*` splat and nested routers use relative paths (e.g. `path=":callId"`).
   - `Link as RouterLink` imports preserved.

5. **Env-var rename** — `REACT_APP_*` → `VITE_*` in:
   - `src/aws-exports.js` and other consumer modules (`import.meta.env.VITE_*`).
   - `lma-ai-stack/deployment/lma-ai-stack.yaml` CodeBuild env vars and the
     `LocalUITestingEnv` output.
   - The CodeBuild `build` phase now writes `.env.production` from the
     `VITE_*` environment variables so Vite picks them up during `npm run build`.

## Things intentionally left alone

- The `aws-exports.js` file is still a handwritten module (not Amplify-CLI-generated). It now simply maps `import.meta.env.VITE_*` onto the v6 `Amplify.configure` shape.
- `@aws-amplify/ui-react/styles.css` is imported once in `App.jsx`.
- Python Lambdas that rely on `transcript_enrichment_layer`'s `get_owner_from_jwt` (`CallEventProcessorFunction`, `MeetingControlsResolverFunction`) now read `VITE_AWS_REGION` / `VITE_USER_POOL_ID`. The layer's `eventprocessor.py` was updated accordingly.

## Verification

- `npm run lint` — clean.
- `npm run build` — succeeds (`build/` artefacts generated).
- The build chunking config in `vite.config.js` splits the bundle into
  `react-vendor`, `aws-amplify`, `aws-sdk`, `cloudscape`, and the app’s own
  `index` chunk.
