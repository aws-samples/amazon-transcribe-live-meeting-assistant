const js = require('@eslint/js');
const tseslint = require('@typescript-eslint/eslint-plugin');
const tsParser = require('@typescript-eslint/parser');

// Flat config migrated from the legacy .eslintrc when upgrading to ESLint 10 and
// typescript-eslint 8. typescript-eslint v8 removed several stylistic rules
// (brace-style, indent, quotes) that were formatting-only extensions of the core
// rules, so those are expressed with the equivalent core ESLint rules here.
module.exports = [
    {
        ignores: ['dist/**', 'node_modules/**', 'eslint.config.js'],
    },
    js.configs.recommended,
    ...tseslint.configs['flat/recommended'],
    {
        files: ['**/*.ts'],
        languageOptions: {
            parser: tsParser,
            sourceType: 'module',
            globals: {
                process: 'readonly',
                console: 'readonly',
            },
        },
        plugins: {
            '@typescript-eslint': tseslint,
        },
        rules: {
            '@typescript-eslint/array-type': ['error', { default: 'array-simple' }],
            'brace-style': ['error', '1tbs'],
            indent: ['error', 4],
            quotes: ['error', 'single'],
            curly: ['error', 'all'],
            'object-curly-spacing': ['error', 'always'],
            semi: ['error', 'always'],
        },
    },
];
