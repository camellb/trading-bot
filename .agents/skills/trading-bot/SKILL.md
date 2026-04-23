```markdown
# trading-bot Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill provides guidance on developing and maintaining the `trading-bot` TypeScript codebase. It covers the project's coding conventions, file organization, import/export patterns, and testing strategies. The repository does not use a specific framework, focusing instead on idiomatic TypeScript practices for building trading automation logic.

## Coding Conventions

### File Naming
- Use **camelCase** for file names.
  - Example: `tradeManager.ts`, `orderBookHandler.ts`

### Import Style
- Use **absolute imports** for modules.
  - Example:
    ```typescript
    import { TradeManager } from 'services/tradeManager';
    ```

### Export Style
- Both **named** and **default exports** are used, depending on context.
  - Example (named export):
    ```typescript
    export function calculateProfit() { ... }
    ```
  - Example (default export):
    ```typescript
    export default class TradeBot { ... }
    ```

### Commit Patterns
- Commit messages are **freeform** (no enforced prefixes).
- Average commit message length is about 56 characters.

## Workflows

### Adding a New Feature
**Trigger:** When implementing a new trading strategy or bot feature  
**Command:** `/add-feature`

1. Create a new TypeScript file using camelCase naming.
2. Use absolute imports for dependencies.
3. Export your main class or function (default or named as appropriate).
4. Write or update corresponding tests in a `.test.ts` file.
5. Commit your changes with a clear, descriptive message.

### Fixing a Bug
**Trigger:** When resolving an issue or bug in the codebase  
**Command:** `/fix-bug`

1. Locate the relevant TypeScript file.
2. Apply the fix, following coding conventions.
3. Update or add tests in the related `.test.ts` file to cover the fix.
4. Commit your changes with a descriptive message about the bug and fix.

### Running Tests
**Trigger:** When verifying code correctness  
**Command:** `/run-tests`

1. Identify all test files matching the `*.test.*` pattern.
2. Use the project's test runner (framework is unknown; check documentation or scripts).
3. Run the tests and review results.
4. Address any failing tests before committing.

## Testing Patterns

- Test files use the `*.test.*` naming convention (e.g., `tradeManager.test.ts`).
- The testing framework is **unknown**; check project documentation or `package.json` for details.
- Tests should cover both new features and bug fixes.
- Example test file:
  ```typescript
  // tradeManager.test.ts
  import { calculateProfit } from 'services/tradeManager';

  test('calculates profit correctly', () => {
    expect(calculateProfit(100, 120)).toBe(20);
  });
  ```

## Commands
| Command      | Purpose                                  |
|--------------|------------------------------------------|
| /add-feature | Scaffold and implement a new feature      |
| /fix-bug     | Apply and test a bug fix                 |
| /run-tests   | Run all test files in the codebase       |
```
