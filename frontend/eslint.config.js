// ESLint flat config (ESLint 9+).
//
// Replaces the old `.eslintrc.cjs`. The move was not cosmetic: ESLint 8 carried
// `@humanwhocodes/config-array` → `glob@7` → `minimatch@3` →
// `brace-expansion@1`, and that last package has an unpatched DoS advisory for
// every version at or below 5.0.7. There is no fix inside the 1.x line, and
// forcing 5.x through an `overrides` entry does not work — 5.x is ESM-first and
// `require()` returns a non-callable object, so `minimatch@3` breaks at load.
// Upgrading ESLint removes the whole chain instead of papering over it.
import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';

export default tseslint.config(
  {
    // Flat config has no `ignorePatterns`; a config object with only `ignores`
    // sets them globally. `dist` is build output and `node_modules` is implicit.
    ignores: ['dist/**', 'node_modules/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,

      // eslint-plugin-react-hooks 7 added the React Compiler correctness rules.
      // They are ERRORS: each one marks a pattern React cannot render reliably
      // — components rebuilt every render (which remounts inputs mid-keystroke),
      // setState cascades inside effects, refs and impure calls read during
      // render. They were fixed rather than downgraded.

      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': 'warn',

      // An empty `catch {}` on a best-effort teardown (closing an already-dead
      // socket, revoking an object URL) is deliberate: there is nothing useful
      // to do with the error and nothing to log. Every empty block in this tree
      // is one of those — verified, not assumed. Empty `if`/loop bodies remain
      // errors, which is the part of this rule that catches real mistakes.
      'no-empty': ['error', { allowEmptyCatch: true }],
    },
  },
);
