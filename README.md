# spec-mcp

An [MCP](https://modelcontextprotocol.io) server that turns a project's **documentation,
user flows and requirements** into tools an agent can query, link and validate — with a
coverage report that always states its denominator.

- **Docs**: markdown files, chunked by heading, searched with BM25 (pure Python, no deps).
- **Requirements**: read from hand-written markdown (any line carrying an ID that matches
  a configurable regex) *and* from JSON collections. Written as JSON only.
- **Flows**: read from markdown sections (configurable heading regex) *and* from JSON
  collections. Written as JSON only.
- **Coverage & validation**: which requirements have a status, a flow, a test, a doc;
  duplicate / dangling IDs; links to things that do not exist.

Nothing project-specific lives in the code. Every format decision is a config key.

## Run it

```bash
uvx --from git+https://github.com/bryndalski/spec-mcp spec-mcp
```

Claude Code `.mcp.json`:

```json
{
  "mcpServers": {
    "spec": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/bryndalski/spec-mcp", "spec-mcp"],
      "env": { "SPEC_CONFIG": "${CLAUDE_PROJECT_DIR}/.spec-mcp.json" }
    }
  }
}
```

The server resolves relative paths against its working directory (the project root in
Claude Code). `SPEC_CONFIG` points at the config file; when it is missing, defaults apply.

## Configuration

`.spec-mcp.json` — every key is optional and merged over these defaults:

```json
{
  "docs": {
    "globs": ["docs/**/*.md"],
    "exclude": ["**/node_modules/**"]
  },
  "requirements": {
    "globs": ["docs/requirements/**/*.md", "docs/requirements/**/*.json"],
    "exclude": ["**/node_modules/**"],
    "id_pattern": "\\b[A-Z][A-Z0-9]{1,7}-R?\\d{1,4}\\b",
    "status_pattern": "\\b(TODO|IN PROGRESS|DONE|BLOCKED|DEPRECATED)\\b",
    "write_file": "docs/requirements/requirements.json"
  },
  "flows": {
    "globs": ["docs/flows/**/*.md", "docs/flows/**/*.json"],
    "exclude": ["**/node_modules/**"],
    "heading_pattern": "^##\\s+(.+)$",
    "write_file": "docs/flows/flows.json"
  },
  "links_file": ".spec-links.json",
  "index_file": ".spec-index.json"
}
```

| Key | Meaning |
|---|---|
| `docs.globs` / `docs.exclude` | Markdown files to index for `docs_*`. Globs may be absolute. |
| `*.exclude` | Every collection (`docs`, `requirements`, `flows`) takes an `exclude` list of globs, matched against the absolute and the project-relative path. Default: `["**/node_modules/**"]`. |
| `requirements.globs` | `*.md` files are parsed read-only; `*.json` files are JSON collections. |
| `requirements.id_pattern` | Regex for requirement IDs (e.g. `ACT-R1`, `REQ-042`). A line with a match *is* a requirement; the first match is its ID, further matches are `refs`. |
| `requirements.status_pattern` | Regex (first group = status) searched on the same line. |
| `requirements.write_file` | JSON collection that `req_upsert` / `spec_import_markdown` write to. Always indexed. |
| `flows.globs` | `*.md` files are split into flows by `heading_pattern`; `*.json` files are JSON collections. |
| `flows.heading_pattern` | Regex matched per line; group 1 (or the heading text) is the flow title, slugified into its id. |
| `flows.write_file` | JSON collection that `flow_upsert` / `spec_import_markdown` write to. Always indexed. |
| `links_file` | Sidecar for links on **markdown-sourced** requirements (JSON items carry links inline). |
| `index_file` | Cache of the parsed collections, fingerprinted by file list + mtimes + size + config. Rebuilt lazily when stale and after every write. Add it to `.gitignore`. |

## Tools

| Tool | What it does |
|---|---|
| `spec_config()` | Effective config, counts per collection, index age. |
| `docs_search(query, limit=10, glob=None)` | BM25 over heading chunks → `file, heading, line, score, snippet` + `searched_chunks`, `searched_files`. |
| `docs_get(path, heading=None)` | Whole file or one section with its line range. |
| `docs_list(glob=None, limit=200)` | Files with title (first H1) and size + `total`. |
| `req_list(domain, status, with_flow, with_test, limit)` | Filtered requirements + `total`. |
| `req_get(id)` | One requirement: refs, evidence, effective links, flows mentioning it, duplicate definitions. |
| `req_upsert(id, text, status, domain, file, title, tags, acceptance)` | Create/update a JSON item (see schema). Refuses IDs defined in another file. |
| `req_link(id, target_type, target)` | Link to a `flow` id, `doc` path or `test` path. Inline for JSON items, sidecar for markdown items. Flow/doc targets must exist. |
| `req_coverage(domain=None)` | `total, with_status, with_flow, with_test, with_doc, by_domain, uncovered, denominator`. |
| `flow_list(limit)` / `flow_get(id)` | Flows with parsed steps `{n, role, screen, action, evidence}` and requirement IDs. |
| `flow_upsert(id, title, steps, requirements, roles, file)` | Create/replace a JSON flow item. Refuses ids defined in markdown. |
| `spec_import_markdown(collection, glob, target_file, dry_run=True)` | Convert markdown-parsed requirements or flows into the JSON schema (keeps `source.file/line`). `dry_run=False` merges into the target without duplicating ids. |
| `spec_validate()` | Duplicate IDs, dangling IDs (referenced but undefined, incl. from flows), sidecar links to missing targets. |
| `spec_index()` | Force rebuild and regenerate `INDEX.md` in every requirements/flows directory. |

Errors never raise: `{"error": {"category": "not_found|validation|io|config", "message", "retryable"}}`.
Every list answer says what was scanned, so an empty result is distinguishable from
"nothing was looked at".

## File formats

### Markdown (read-only)

Requirements — any line with an ID, in a table or a bullet:

```markdown
| ACT-R1 | funnelStage is derived on every write | `activation.types.ts:5-6` |
- INV-R1 · Admin creates an invitation · TODO
```

Parsed: `id`, `text` (row with the ID and pipes stripped), `status`, `domain` (file stem),
`refs` (other IDs on the line), `evidence` (code refs like `file.ts:12-14`; anything
matching `.spec.` / `.test.` / `__tests__` / `e2e` counts as a test).

Flows — one section per heading match, steps from the first table (columns matched by
name: `#`, `role`, `screen`, `action`/`step`, `evidence`/`proof`, also in Polish) or a
numbered list; requirement IDs anywhere in the section.

### JSON collections (read + write)

`{"items": [...]}` or a bare array. A JSON file in scope that has any other shape (or
is not valid JSON) is skipped and reported under `spec_config().warnings` with
`category: "config"` — it never aborts indexing. Written files are pretty-printed with 2-space indent,
fixed key order, items sorted by `id` — so diffs stay small and `jq`/prettier-friendly.

Requirement item:

```json
{
  "id": "BIL-R1",
  "title": "Invoice monthly",
  "text": "Invoices are issued monthly per organization",
  "status": "DONE",
  "domain": "billing",
  "tags": ["billing"],
  "acceptance": ["Given an active org, when the month ends, then an invoice exists"],
  "links": { "flows": ["checkout"], "docs": ["docs/BILLING.md"], "tests": ["billing.spec.ts"] },
  "source": { "file": "docs/requirements/domains/billing.md", "line": 12 }
}
```

Flow item:

```json
{
  "id": "checkout",
  "title": "Checkout",
  "roles": ["owner"],
  "requirements": ["BIL-R1"],
  "steps": [
    { "n": "1", "role": "owner", "screen": "/billing", "action": "pay invoice", "evidence": "pay.e2e.ts" }
  ],
  "source": { "file": "docs/flows/PROCESSES.md", "line": 3 }
}
```

`source` is optional; `spec_import_markdown` fills it so a JSON item still points at the
prose it came from.

### Non-destructive by design

- Markdown is **never rewritten**. Writers (`req_upsert`, `flow_upsert`,
  `spec_import_markdown`, `req_link`) only touch JSON collection files and the sidecar.
- An ID defined in markdown cannot be shadowed by a JSON write — the tool refuses with a
  `validation` error naming the file.
- Links for markdown items live in `links_file` (`{"ACT-R1": {"flows": [], "docs": [],
  "tests": []}}`); JSON items carry `links` inline. `req_get`, `req_list` and
  `req_coverage` merge both.
- `INDEX.md` files written by `spec_index` carry a `generated by spec-mcp` marker and are
  skipped when parsing, so they never become requirements themselves.

## Development

```bash
uv sync
uv run pytest -q
uv run spec-mcp        # stdio
```

MIT.
