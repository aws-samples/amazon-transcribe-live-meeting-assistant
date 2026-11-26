/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import eslint from '@eslint/js';
import tseslint from 'typescript-eslint';
import stylistic from '@stylistic/eslint-plugin';

// Flat config (ESLint 10 / typescript-eslint 8). Migrated from the legacy
// .eslintrc. Formatting rules moved to @stylistic since eslint/tseslint
// dropped stylistic rules in v9+/v8.
// list-style replaces the deprecated object-curly-spacing. Only braces are
// checked: a space inside single-line `{ }`, as before, and one item per line
// in a multi-line `{ }` whose first item starts on its own line. Parentheses,
// brackets and angle brackets stay unchecked.
const LIST_STYLE_BRACES = {
    singleLine: { spacing: 'always' },
    overrides: { '()': 'off', '[]': 'off', '<>': 'off' },
};

export default tseslint.config(
    {
        ignores: ['dist/**', 'node_modules/**', 'eslint.config.mjs'],
    },
    eslint.configs.recommended,
    ...tseslint.configs.recommended,
    {
        files: ['**/*.ts'],
        plugins: {
            '@stylistic': stylistic,
        },
        languageOptions: {
            globals: {
                process: 'readonly',
                Buffer: 'readonly',
                console: 'readonly',
                __dirname: 'readonly',
                module: 'readonly',
                require: 'readonly',
            },
        },
        rules: {
            '@typescript-eslint/array-type': ['error', { default: 'array-simple' }],
            '@stylistic/brace-style': ['error', '1tbs'],
            '@stylistic/indent': ['error', 4],
            '@stylistic/quotes': ['error', 'single'],
            '@stylistic/semi': ['error', 'always'],
            '@stylistic/list-style': ['error', LIST_STYLE_BRACES],
            'curly': ['error', 'all'],
        },
    },
    {
        // These object literals are laid out as compact tables, several
        // entries per line, so list-style skips object literals here.
        files: ['src/config/configuration-store.ts', 'test/local-e2e-client.ts'],
        rules: {
            '@stylistic/list-style': ['error', {
                ...LIST_STYLE_BRACES,
                overrides: { ...LIST_STYLE_BRACES.overrides, ObjectExpression: 'off' },
            }],
        },
    }
);
