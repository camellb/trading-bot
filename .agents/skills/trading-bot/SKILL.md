```markdown
# trading-bot Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill teaches you the core development patterns and conventions used in the `trading-bot` Python repository. You'll learn how to structure files, write and import code, and follow the project's conventions for testing and commit messages. This guide is ideal for contributors who want to maintain consistency and quality in the codebase.

## Coding Conventions

### File Naming
- Use **snake_case** for all file and module names.
  - Example: `trade_executor.py`, `order_manager.py`

### Import Style
- Use **relative imports** within the package.
  - Example:
    ```python
    from .utils import calculate_profit
    from .models import Trade
    ```

### Export Style
- Use **named exports** (explicitly define what is exported from modules).
  - Example:
    ```python
    # In trade_executor.py
    def execute_trade(...):
        ...

    __all__ = ['execute_trade']
    ```

### Commit Messages
- Freeform messages, sometimes with prefixes.
- Average length: ~54 characters.
  - Example: `Add new trade execution logic for limit orders`

## Workflows

### Adding a New Feature
**Trigger:** When implementing a new trading strategy or bot feature  
**Command:** `/add-feature`

1. Create a new module using snake_case (e.g., `new_strategy.py`).
2. Write your feature using relative imports for any shared utilities or models.
3. Define named exports for public functions or classes.
4. Add or update corresponding test files (see Testing Patterns).
5. Commit your changes with a clear, descriptive message.

### Refactoring Existing Code
**Trigger:** When improving code structure or readability  
**Command:** `/refactor-code`

1. Identify the module(s) to refactor.
2. Update code to use relative imports and snake_case naming if needed.
3. Ensure all exports are named and explicitly listed in `__all__`.
4. Run or update tests to verify no regressions.
5. Commit with a message describing the refactor.

### Writing and Running Tests
**Trigger:** When adding new features or fixing bugs  
**Command:** `/run-tests`

1. Create or update test files matching the `*.test.*` pattern (e.g., `trade_executor.test.py`).
2. Write tests for new or changed functionality.
3. Use the project's preferred (unknown) test framework.
4. Run tests to ensure correctness.
5. Commit with a message describing the tests added or updated.

## Testing Patterns

- Test files follow the pattern: `*.test.*` (e.g., `order_manager.test.py`).
- The specific test framework is not detected, but tests are likely Python functions or classes.
- Place tests alongside or within a dedicated test directory.
- Example test file:
  ```python
  # order_manager.test.py
  from .order_manager import create_order

  def test_create_order():
      order = create_order(...)
      assert order is not None
  ```

## Commands
| Command        | Purpose                                             |
|----------------|-----------------------------------------------------|
| /add-feature   | Start the workflow for adding a new feature         |
| /refactor-code | Begin refactoring existing code                     |
| /run-tests     | Run all tests in the repository                     |
```
