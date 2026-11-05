// "No magic numbers or strings" (AGENTS.md) for the repo's JavaScript and TypeScript.
// Allowed without a name: 0, 1, -1 and "". The hits that existed when the rule arrived
// are in scripts/lint-js/eslint-suppressions.json; CI fails on any other. `run.sh --suppress-all`
// rewrites that file after a burn-down (scripts/lint-js/run.sh).
import { createRequire } from 'node:module';

// The linter's own packages live in scripts/lint-js, away from the docs site's package.json.
const tseslint = createRequire(new URL('./scripts/lint-js/', import.meta.url))('typescript-eslint');

export default [
  tseslint.configs.base,
  {
    files: ['**/*.{js,mjs,ts,tsx}'],
    rules: {
      'no-magic-numbers': ['error', { ignore: [0, 1, -1], ignoreArrayIndexes: true }],
      'no-restricted-syntax': ['error',
        {
          selector: "BinaryExpression[operator=/^[!=]==?$/] > Literal[raw=/^['\"].+['\"]$/]",
          message: 'String literal in a comparison: give it a name (a const, or a frozen object of the values).',
        },
        {
          selector: "SwitchCase > Literal[raw=/^['\"].+['\"]$/]",
          message: 'String literal in a case: give it a name.',
        },
      ],
    },
  },
];
