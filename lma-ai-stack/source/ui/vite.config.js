/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  plugins: [
    react({
      jsxRuntime: 'automatic',
      include: '**/*.{js,jsx}',
    }),
  ],

  esbuild: {
    jsx: 'automatic',
    // Allow JSX syntax inside `.js` files (legacy CRA convention).
    loader: 'jsx',
    include: /src\/.*\.[jt]sx?$/,
    exclude: [],
  },

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
        manualChunks: {
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
        },
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
    esbuildOptions: {
      loader: {
        '.js': 'jsx',
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
