# ADR 0001 — One Python package, local-only stack

**Status:** accepted

**Context.** The brief suggested a monorepo of services and packages. With one language and a
small team of one, separate distributions add build and import overhead without isolation benefits.

**Decision.** One package, `fraudplat`, with subpackages (`contracts`, `scoring`, `storage`,
`api`, later `features`, `worker`, `publisher`). Services are separate *entry points* of the same
package, run as separate processes/containers. Everything runs locally under Docker Compose
(Colima runtime) with services bound to 127.0.0.1. Plain SQL through psycopg 3; Alembic runs
raw-SQL migrations.

**Consequences.** Shared code cannot drift between services. If a service later needs an
independent release cadence, it can be split out; nothing in the layout prevents that.
