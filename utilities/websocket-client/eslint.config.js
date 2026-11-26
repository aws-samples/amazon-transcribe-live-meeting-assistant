const js = require('@eslint/js');
const tseslint = require('@typescript-eslint/eslint-plugin');
const tsParser = require('@typescript-eslint/parser');
const stylistic = require('@stylistic/eslint-plugin');

// Flat config migrated from the legacy .eslintrc when upgrading to ESLint 10 and
// typescript-eslint 8. typescript-eslint v8 removed several stylistic rules
// (brace-style, indent, quotes) that were formatting-only extensions of the core
// rules, so those are expressed with the equivalent core ESLint rules here.
// Brace spacing uses @stylistic/list-style, which replaces the deprecated
// object-curly-spacing: it checks a space inside single-line `{ }` and one item
// per line in a multi-line `{ }` whose first item starts on its own line.
// Parentheses, brackets and angle brackets stay unchecked.
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
            '@stylistic': stylistic,
        },
        rules: {
            '@typescript-eslint/array-type': ['error', { default: 'array-simple' }],
            'brace-style': ['error', '1tbs'],
            indent: ['error', 4],
            quotes: ['error', 'single'],
            curly: ['error', 'all'],
            '@stylistic/list-style': ['error', {
                singleLine: { spacing: 'always' },
                overrides: { '()': 'off', '[]': 'off', '<>': 'off' },
            }],
            semi: ['error', 'always'],
        },
    },
];
