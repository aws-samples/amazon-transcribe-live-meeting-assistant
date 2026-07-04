/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import { defineConfig, transformWithOxc } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

// Groupings for splitting vendor code into separate chunks. Each entry maps a
// chunk name to the set of top-level node_modules package names that belong to
// it. Vite 8 (Rolldown) requires `manualChunks` to be a function rather than an
// object, so we resolve the package name from the module id and look it up here.
const manualChunkGroups = {
  'aws-amplify': ['aws-amplify', '@aws-amplify/ui-react'],
  'aws-sdk': [
    '@aws-sdk/client-cognito-identity',
    '@aws-sdk/client-lambda',
    '@aws-sdk/client-sfn',
    '@aws-sdk/client-ssm',
    '@aws-sdk/client-translate',
    '@aws-sdk/s3-request-presigner',
  ],
  cloudscape: ['@cloudscape-design/components', '@cloudscape-design/global-styles'],
  'react-vendor': ['react', 'react-dom', 'react-router-dom'],
};

// Extract the top-level package name (including scope) from a module id that
// lives inside node_modules, e.g.
//   /abs/node_modules/@aws-sdk/client-lambda/dist/index.js -> @aws-sdk/client-lambda
//   /abs/node_modules/react-dom/index.js                   -> react-dom
const getPackageName = (id) => {
  const marker = 'node_modules/';
  const idx = id.lastIndexOf(marker);
  if (idx === -1) return null;
  const rest = id.slice(idx + marker.length);
  const parts = rest.split('/');
  return parts[0].startsWith('@') ? `${parts[0]}/${parts[1]}` : parts[0];
};

const manualChunks = (id) => {
  const pkg = getPackageName(id);
  if (!pkg) return undefined;
  const entry = Object.entries(manualChunkGroups).find(([, pkgs]) => pkgs.includes(pkg));
  return entry ? entry[0] : undefined;
};

// --- JSX-in-.js support for Vite 8 (Rolldown + Oxc) --------------------------
// This project follows the legacy CRA convention of writing JSX inside plain
// `.js` files. Vite 7 handled that via the top-level `esbuild` block
// (`loader: 'jsx'`) and `optimizeDeps.esbuildOptions.loader`, both of which
// Vite 8 ignores now that the pipeline is Rolldown + Oxc.
//
// Oxc derives the source language from the file extension, so a `.js` file is
// parsed as plain JS and JSX syntax is rejected ("JSX syntax is disabled").
// The native bundled-build transform used by `@vitejs/plugin-react` does NOT
// honour an `oxc.lang` / per-file `lang` override, so relying on plugin-react
// alone is not enough for `.js` files.
//
// Vite does, however, export `transformWithOxc`, whose JS API *does* honour
// `lang: 'jsx'`. We use it in a small `enforce: 'pre'` plugin that transforms
// JSX inside `src/**/*.js` to plain JS (automatic React runtime) before the
// rest of the pipeline sees the module. `.jsx` files are left to
// `@vitejs/plugin-react`, which handles them (and React Fast Refresh) natively.
const jsxInJsPlugin = () => {
  const jsFileRE = /\.js$/;
  return {
    name: 'lma:jsx-in-js',
    enforce: 'pre',
    async transform(code, id) {
      const [filepath] = id.split('?');
      if (!jsFileRE.test(filepath) || filepath.includes('/node_modules/')) return null;
      const result = await transformWithOxc(code, filepath, {
        lang: 'jsx',
        jsx: {
          runtime: 'automatic',
          importSource: 'react',
        },
      });
      return { code: result.code, map: result.map };
    },
  };
};

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  plugins: [
    jsxInJsPlugin(),
    // `@vitejs/plugin-react` (v5+, Vite 8 compatible) drives the JSX transform
    // for `.jsx` and provides React Fast Refresh. `include` widens Fast Refresh
    // to `.js` files too. On Vite 8 the plugin wires up Oxc's automatic JSX
    // runtime and `optimizeDeps.rolldownOptions.transform.jsx`, replacing the
    // Vite 7 `esbuild` / `optimizeDeps.esbuildOptions` approach.
    react({
      jsxRuntime: 'automatic',
      include: '**/*.{js,jsx}',
    }),
  ],

  server: {
    port: 3000,
    open: true,
    cors: true,
    host: true,
  },

  build: {
    outDir: 'build',
    sourcemap: mode === 'development' ? 'inline' : false,
    chunkSizeWarningLimit: 3000,
    rollupOptions: {
      output: {
        manualChunks,
      },
    },
    target: 'esnext',
  },

  resolve: {
    alias: {
      '@': resolve(__dirname, './src'),
      './runtimeConfig': './runtimeConfig.browser',
    },
    extensions: ['.mjs', '.js', '.jsx', '.json'],
  },

  define: {
    // Shim process.env for 3rd-party packages that still reference it.
    'process.env': {},
    global: 'globalThis',
  },

  optimizeDeps: {
    include: [
      'react',
      'react-dom',
      'react-router-dom',
      'aws-amplify',
      '@aws-amplify/ui-react',
      '@cloudscape-design/components',
      '@cloudscape-design/global-styles',
    ],
    // Vite 8 pre-bundles deps with Rolldown/Oxc instead of esbuild, so the
    // deprecated `optimizeDeps.esbuildOptions.loader` is replaced by
    // `rolldownOptions`. Enabling the automatic JSX runtime keeps pre-bundled
    // deps consistent with the app transform. (`@vitejs/plugin-react` also sets
    // this; declaring it here makes the intent explicit and independent of the
    // plugin's internals.)
    rolldownOptions: {
      transform: {
        jsx: {
          runtime: 'automatic',
        },
      },
    },
  },

  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/setupTests.js',
    include: ['src/**/*.test.{js,jsx}'],
  },
}));
