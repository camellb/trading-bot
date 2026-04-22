# @delfi/contracts

Source-of-truth data contracts for the bot <-> web boundary.

Each JSON Schema in `schemas/` is a canonical shape that crosses a
runtime boundary:

| Schema                          | Produced by                              | Consumed by                              |
|---------------------------------|------------------------------------------|------------------------------------------|
| `risk_config.schema.json`       | `apps/web/app/dashboard/risk/`           | `apps/bot/engine/user_config`            |
| `position.schema.json`          | `apps/bot/execution/pm_executor`         | `apps/web/app/api/positions/`            |
| `market_evaluation.schema.json` | `apps/bot/engine/polymarket_evaluator`   | `apps/web/app/api/evaluations/`          |
| `trade.schema.json`             | `apps/bot/bot_api`                       | `apps/web/app/dashboard/performance/`    |

## Status

Phase C established the schemas as documentation. Full generator-based
wiring (pydantic in `python/`, TS types in `typescript/`) is a Phase F
deliverable — it is only valuable once the two apps run in separate
deployments (Railway + Vercel) and can drift out of sync.

Until then: when you change a shape on one side of the boundary, update
the matching schema here, then mirror the change on the other side.
