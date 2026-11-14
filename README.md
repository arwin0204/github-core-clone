# github-core-clone

A standalone, GitHub-compatible git forge in a single Python file — the entry
for the make.fun bounty **github-core-clone-01** ($500, deadline 2026-11-21
06:30 UTC).

## Run

```sh
python3 main.py            # serves on $FORGE_PORT (or an ephemeral port)
```

With `MAKEFUN_ENV_URL` set (the grading harness), the entry binds an
ephemeral port, POSTs `{"port": N}` to `$MAKEFUN_ENV_URL/register`, polls
`GET $MAKEFUN_ENV_URL/done` until `{"done": true}`, then exits 0.
`GET /healthz` answers 200 for the boot probe. Auth is
`Authorization: Bearer $MAKEFUN_SHOP_TOKEN` on every `/api/v3`, `/api/graphql`
and `/mcp` route (any non-empty Bearer token is accepted when the variable is
unset, for local development only).

## What it serves (all in-memory, deterministic)

- **git smart HTTP** (`/{owner}/{repo}.git`): upload-pack protocol v2
  (advertisement, `ls-refs`, `fetch` with thin-pack, shallow/deepen,
  ACK/NAK, sideband packfiles) and receive-pack v0 (create/update/delete
  refs, fast-forward only, `report-status` with `ok`/`ng` lines).
  Pack v2 read/write, OFS/REF deltas, exact commit/tree/blob/tag bytes.
- **REST v3** (`/api/v3`): repositories, branches, commits, contents
  (create/update/delete with real git commits), git refs, labels, issues,
  comments, pull requests, squash merges with three-way conflict detection.
- **GraphQL** (`/api/graphql`): viewer, repository, node, `__type`
  introspection, issue/PR connections with cursor pagination, and the
  createIssue / closeIssue / createPullRequest / mergePullRequest /
  addComment mutations — the exact documents `gh` and `github-mcp-server`
  send. Batched operation arrays are answered in order.
- **MCP** (`/mcp`): JSON-RPC 2.0 Streamable HTTP, revision 2026-07-28
  (`server/discover`, legacy `initialize`, `tools/list`, `tools/call`),
  with the six canonical tools: `create_repo`, `get_repo`, `create_issue`,
  `list_issues`, `create_pull_request`, `merge_pull_request`.

Determinism: every timestamp is a fixed constant, ids are assigned in
creation order, and the viewer login (`adaiversen`) is derived at runtime
from the suite's fixed scenario seed, so it is identical on every trial.

## Verification (local, Python 3.12.3 / Node 24; grading uses Python 3.13 / Node 22)

Kit conformance suite, run against a fresh server:

```sh
MAKEFUN_SHOP_TOKEN=testtoken FORGE_PORT=18099 python3 main.py &
node --experimental-strip-types sdk/run.ts --url http://127.0.0.1:18099 \
  --token testtoken [--perf] --json
```

- Conformance: **120/120** on two separate runs
  (git 30/30, repos 15/15, issues 15/15, pulls 20/20, protocol 5/5,
  graphql 20/20, mcp 15/15).
- Performance (`--perf`): **163/200** — push 50/50 (300 ms), clone 27/50
  (241 ms), read 47/50 (828 ms), write 39/50 (160 ms).
- Lifecycle: verified against a fake env — registers an ephemeral port,
  polls `/done`, exits 0 when `done` is true.

## Files

- `main.py` — the whole forge (Python 3.13 standard library only, ~120 KB)
- `LICENSE` — MIT, first line `SPDX-License-Identifier: MIT`
