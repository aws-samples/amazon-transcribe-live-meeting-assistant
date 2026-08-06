/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import eslint from '@eslint/js';
import tseslint from 'typescript-eslint';
import stylistic from '@stylistic/eslint-plugin';

// Flat config (ESLint 10 / typescript-eslint 8). Migrated from the legacy
// .eslintrc. Formatting rules moved to @stylistic since eslint/tseslint
// dropped stylistic rules in v9+/v8.
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
            '@stylistic/object-curly-spacing': ['error', 'always'],
            'curly': ['error', 'all'],
        },
    }
);
