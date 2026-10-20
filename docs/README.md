# RouteForge documentation

RouteForge is a self-hostable, OpenAI-compatible, multi-tenant model router and
policy gateway. For the product's rules and module architecture, read
[`../CLAUDE.md`](../CLAUDE.md); for a two-minute overview, [`../README.md`](../README.md).

## Start here

| You are | Read first |
|---|---|
| An **operator** setting RouteForge up | [`guides/getting-started.md`](guides/getting-started.md) |
| A **developer** integrating an application | [`guides/integrating-an-app.md`](guides/integrating-an-app.md) |
| An **engineer** evaluating the design | [`architecture/logical-and-request-flow.md`](architecture/logical-and-request-flow.md) |
| A **contributor** | [`../CLAUDE.md`](../CLAUDE.md), then [`backlog/bugs.md`](backlog/bugs.md) |

## What's in each directory

**The second column is the point of this layout: the path tells you how much to
trust the page.**

| Directory | Contents | How current |
|---|---|---|
| [`guides/`](guides/README.md) | Task-oriented how-to for operators and app developers | Verified against HEAD 2026-09-11 |
| [`reference/`](reference/README.md) | Field, endpoint and metric lookup | Verified against HEAD 2026-09-11 |
| [`architecture/`](architecture/README.md) | How RouteForge works today | **Every claim checkable against HEAD**, verified 2026-09-10 |
| [`architecture/specs/`](architecture/README.md) | Binding contracts for shipped features, cited from code | Verified against their call sites |
| [`backlog/`](backlog/bugs.md) | The single list of open defects and gaps | Live |
