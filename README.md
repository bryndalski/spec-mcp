# spec-mcp

An [MCP](https://modelcontextprotocol.io) server that turns a project's **documentation,
user flows and requirements** into tools an agent can query, link and validate — with a
coverage report that always states its denominator.

- **Docs**: markdown files, chunked by heading, searched with BM25 (pure Python, no deps).
- **Requirements**: read from hand-written markdown (any line carrying an ID that matches
  a configurable regex) *and* from JSON collections. Written as JSON only.
- **Flows**: read from markdown sections (configurable heading regex) *and* from JSON
  collections. Written as JSON only.
- **Data model**: DynamoDB-style tables (keys, GSIs, entities, attributes, pseudo-relations,
  access patterns) kept as JSON, queryable and aggregatable — the tool answers "which
  table / index / repository" instead of the agent rediscovering it from code.
- **UI components**: a per-component registry (`*.registry.json`) made searchable —
  "is there already a component for X?" — and linted against disk, tokens and the index.
- **Designs**: one JSON record per view (route, roles, flow, requirements, artboards,
  brief, components, acceptance, evidence) with status gates checked by `design_validate`.
- **Records**: any JSON collection (`{"records": [...]}`) with a JSON Schema per record —
  claims, campaigns, legal gaps, OKRs, cost lines — listed, filtered, aggregated and
  written back only after validation.
- **Coverage & validation**: which requirements have a status, a flow, a test, a doc;
  duplicate / dangling IDs; links to things that do not exist; data-model lint.

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
  "datamodel": {
    "globs": ["docs/datamodel/*.json"],
    "exclude": ["**/node_modules/**"],
    "max_gsis_per_table": 3,
    "write_file": "docs/datamodel/datamodel.json"
  },
  "components": {
    "globs": ["front-end/vendor/duotio-ui/src/components/*/*.registry.json"],
    "exclude": ["**/node_modules/**"],
    "index_file_globs": ["front-end/vendor/duotio-ui/registry/index.json"],
    "tokens_css": "front-end/vendor/duotio-ui/src/styles/tokens.css",
    "expected_files": [".tsx", ".test.tsx", ".e2e.ts", ".stories.tsx", ".md", ".registry.json", "index.ts"]
  },
  "designs": {
    "globs": ["docs/design/views/*.json"],
    "exclude": ["**/node_modules/**"],
    "write_dir": "docs/design/views",
    "messages_file": "front-end/messages/pl.json"
  },
  "links_file": ".spec-links.json",
  "index_file": ".spec-index.json"
}
```

| Key | Meaning |
|---|---|
| `docs.globs` / `docs.exclude` | Markdown files to index for `docs_*`. Globs may be absolute. |
| `docs.statuses` / `docs.default_hidden_statuses` | Allowed front-matter statuses (default `current, superseded, historical, draft`) and the ones `docs_search` / `docs_list` hide unless asked (default `superseded, historical`). |
| `docs.require_front_matter` | `true`: a file without `status` is `unknown` (never hidden, flagged by `docs_check`). `false` (default): it counts as `current`. |
| `docs.archive_dir` | Where `docs_supersede` moves retired files (default `docs/archive`). |
| `docs.stale_patterns` | `[{id, regex, reason, flags: "i", exclude_statuses: [...]}]` — lines matching are reported by `docs_check` (files with an excluded status are skipped). |
| `docs.evidence_roots` | Directories (relative to the project root) that `flow_verify` / `req_verify` resolve `path:line` evidence against. Empty = the project root. |
| `docs.max_chunk_chars` | Soft size limit of one search chunk (default 1200); longer sections split on paragraphs. |
| `flows.require_screen_role` | `true`: `spec_validate` warns for JSON flow steps with an empty `screen` or `role`. |
| `*.exclude` | Every collection (`docs`, `requirements`, `flows`) takes an `exclude` list of globs, matched against the absolute and the project-relative path. Default: `["**/node_modules/**"]`. |
| `requirements.globs` | `*.md` files are parsed read-only; `*.json` files are JSON collections. |
| `requirements.id_pattern` | Regex for requirement IDs (e.g. `ACT-R1`, `REQ-042`). A line with a match *is* a requirement; the first match is its ID, further matches are `refs`. |
| `requirements.status_pattern` | Regex (first group = status) searched on the same line. |
| `requirements.write_file` | JSON collection that `req_upsert` / `spec_import_markdown` write to. Always indexed. |
| `flows.globs` | `*.md` files are split into flows by `heading_pattern`; `*.json` files are JSON collections. |
| `flows.heading_pattern` | Regex matched per line; group 1 (or the heading text) is the flow title, slugified into its id. |
| `flows.write_file` | JSON collection that `flow_upsert` / `spec_import_markdown` write to. Always indexed. |
| `datamodel.globs` | JSON files with `{"tables": [...]}` or a bare array (schema below). |
| `datamodel.max_gsis_per_table` | `dm_validate` flags tables with more GSIs than this. |
| `datamodel.write_file` | Where `dm_upsert_table` puts a *new* table; existing tables are written back to their owning file. |
| `components.globs` | One registry JSON per component (`{name, import, description, category, props[], variants, tokensUsed[], a11y, examples[], dependsOn[]}`); the component directory is the registry file's parent. |
| `components.index_file_globs` | Library index (`{"$meta": {...}, "components": [{name, ...}]}`) checked both ways against the registry files. |
| `components.tokens_css` | CSS file whose `--token:` definitions `tokensUsed` must come from. |
| `components.expected_files` | Companion files per component; a leading `.` means `<Name><suffix>`, otherwise a literal file name. |
| `designs.globs` / `designs.write_dir` | One record per view, `docs/design/views/<id>.json`; `design_upsert` writes here. |
| `designs.messages_file` | i18n JSON used for dot-path lookup of `i18n_keys` once a design is `implemented`. |
| `links_file` | Sidecar for links on **markdown-sourced** requirements (JSON items carry links inline). |
| `index_file` | Cache of the parsed collections, fingerprinted by file list + mtimes + size + config. Rebuilt lazily when stale and after every write. Add it to `.gitignore`. |

## Tools

| Tool | What it does |
|---|---|
| `spec_config()` | Effective config, counts per collection, index age. |
| `docs_search(query, limit=10, glob=None, status=None, topic=None, include_stale=False)` | BM25 over heading chunks → `file, heading, path (breadcrumb), line, status, score, snippet` + `searched_chunks`, `searched_files`, `hidden_files`. Superseded/historical files are hidden unless `include_stale` or an explicit `status`. |
| `docs_get(path, heading=None)` | Whole file or one whole section (even if split into chunks) with its line range and status. |
| `docs_list(glob=None, limit=200, status=None, topic=None, include_stale=False)` | Files with title, size, status, topic + `total`, `hidden_count`. |
| `docs_check(glob=None, checks=None, limit=500)` | Read-only hygiene audit: `no_status`, `unknown_status`, `superseded_without_target`, `superseded_by_missing`, `supersedes_missing`, `historical_outside_archive`, `dead_link`, `dead_anchor`, `unresolved_path`, `stale_pattern`. Returns `checked`, `by_status`, `counts_by_rule`, `findings`. |
| `docs_upsert(path, front_matter=None, body=None)` | Write front matter (validated: status, ISO `verified_at`) and optionally the body; path must be in `docs.globs`. |
| `docs_supersede(path, by, reason, move_to_archive=True, dry_run=False)` | Mark `superseded`, add the banner, `git mv` into `docs.archive_dir/<topic or first dir>/`. Refuses when `by` does not exist. |
| `flow_verify(id=None, fix=False, include_ok=False, limit=200)` | Check step/async evidence and anchors of JSON flows against the code (see *Verification*). |
| `req_verify(domain=None, fix=False, include_ok=False, limit=200)` | Same for JSON requirements (`text`, `acceptance`, `evidence[]`, `anchors`). |
| `req_list(domain, status, with_flow, with_test, limit)` | Filtered requirements + `total`. |
| `req_get(id)` | One requirement: refs, evidence, effective links, flows mentioning it, duplicate definitions. |
| `req_upsert(id, text, status, domain, file, title, tags, acceptance)` | Create/update a JSON item (see schema). Refuses IDs defined in another file. |
| `req_link(id, target_type, target)` | Link to a `flow` id, `doc` path or `test` path. Inline for JSON items, sidecar for markdown items. Flow/doc targets must exist. |
| `req_coverage(domain=None)` | `total, with_status, with_flow, with_test, with_doc, by_domain, uncovered, denominator`. |
| `flow_list(limit)` / `flow_get(id)` | Flows with parsed steps `{n, role, screen, action, evidence}` and requirement IDs. |
| `flow_upsert(id, title, steps, requirements, roles, file)` | Create/replace a JSON flow item. Refuses ids defined in markdown. |
| `spec_import_markdown(collection, glob, target_file, dry_run=True)` | Convert markdown-parsed requirements or flows into the JSON schema (keeps `source.file/line`). `dry_run=False` merges into the target without duplicating ids. |
| `spec_validate()` | Duplicate IDs, dangling IDs (referenced but undefined, incl. from flows), sidecar links to missing targets. |
| `spec_index()` | Force rebuild and regenerate `INDEX.md` in every requirements/flows directory and in the data-model directory. |
| `dm_tables()` | Tables with pk/sk names and counts of GSIs, entities, attributes, relations, access patterns. |
| `dm_table(name)` | Full record + inbound/outbound pseudo-relations. |
| `dm_relations(table, entity, format="json"\|"mermaid")` | Edge list, or an `erDiagram` for docs. |
| `dm_access_patterns(table, index, operation, repository)` | Patterns with the repository method implementing each. |
| `dm_query(where={table, entity, attribute, index, operation, kind})` | Generic filter → matching tables, attributes, patterns, relations + counts. |
| `dm_aggregate(by, metric="count")` | Group by table / index / entity / operation / repository / kind; `by="table"` also accepts metric `gsis`, `attributes`, `relations`, `patterns`. |
| `dm_validate()` | Too many GSIs, `Scan` patterns, unknown index, relation to unknown table/attribute, GSI key missing from `attributes`, repository file missing on disk, duplicate tables. |
| `dm_upsert_table(table)` / `dm_upsert_pattern(table, pattern)` | Write back to the owning JSON file (pretty, stable order, sorted by name). |
| `ui_components(category, limit)` | Components with category, import line, prop count and present/expected companion files. |
| `ui_component(name)` | Full registry record + which expected files exist, the missing ones, and the `.md` doc path. |
| `ui_find(query, limit)` | BM25 over name / description / category / prop names+desc / example titles — "is there already a component for X?". |
| `ui_validate()` | Missing companion files, `tokensUsed` not defined in `tokens_css`, components not in the index / index entries not on disk, `dependsOn` naming unknown (PascalCase) components. |
| `ui_aggregate(by="category"\|"token"\|"dependsOn")` | Counts per bucket. |
| `design_list(status)` / `design_get(id)` | Design records; `status` ∈ brief, designed, specified, implemented, verified. |
| `design_upsert(record)` | Merge by id into `write_dir/<id>.json` (pretty, stable key order). |
| `design_validate(id=None)` | duotio-ui component not in registry; feature/new-duotio-ui without `path`; unknown requirement ids; ≥ designed: `artifact_url` and 390 light+dark artboards required; ≥ specified: non-empty `acceptance`; ≥ implemented: every `i18n_keys` entry resolvable in `messages_file`. |
| `design_coverage()` | Records per status, components by source, denominators. |
| `rec_collections()` | Configured record collections: count, files, schema, id field, file-level problems. |
| `rec_list(collection, where, limit, fields)` | Records filtered by field equality (list fields match on containment), projected; `matched` vs `total`. |
| `rec_get(collection, id)` | One record (not-found lists available ids). |
| `rec_upsert(collection, record, file=None)` | Validate against the schema **before** writing; replace in place by id or append to `file` / `write_file` / the single matched file. Full replace, not merge. |
| `rec_validate(collection=None)` | Schema violations with `json_path`, missing id, duplicate ids, empty/malformed files, missing schema; `checked` + `files` as denominator. |
| `rec_aggregate(collection, by, metric="count"\|"sum:f"\|"avg:f", where)` | Group by a field (list fields count per element) with `denominator`. |

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

### Data model (read + write)

`docs/datamodel/*.json` — `{"tables": [...]}` or a bare array. One table:

```json
{
  "name": "companies",
  "source": "backend/terraform/modules/dynamodb/companies.tf:12",
  "description": "One row per organization",
  "pk": { "name": "PK", "type": "S" },
  "sk": { "name": "SK", "type": "S" },
  "gsis": [{ "name": "GSI_COMPANY_URL", "pk": "companyUrl", "sk": null, "projection": "ALL", "attributes": [] }],
  "ttl": "expiresAt",
  "stream": false,
  "entities": ["Company"],
  "attributes": [{ "name": "companyUrl", "type": "S", "required": true, "pii": false, "description": "kebab slug" }],
  "relations": [{ "from": "locations.companyId", "to": "companies.PK", "kind": "belongs_to", "enforced_by": "code", "note": "tenant scope" }],
  "access_patterns": [{ "name": "company by url", "operation": "Query", "index": "GSI_COMPANY_URL", "key": "companyUrl = :url", "repository": "src/repos/company.repository.ts:getByUrl", "description": "" }]
}
```

`projection` ∈ `ALL | KEYS_ONLY | INCLUDE`; `kind` ∈ `belongs_to | has_many | references`;
`enforced_by` ∈ `code | none`; `operation` ∈ `Query | GetItem | BatchGet | Put | Update |
Delete | Transact | Scan`. `relations` are **pseudo-relations**: DynamoDB has no foreign
keys, these are the links the code relies on, written down once so every agent sees the
same picture. `repository` is `path:method`; `dm_validate` checks that the path exists.

**Tool over self-discovery.** Reading Terraform, repositories and Lambda handlers to
re-derive "which index serves this query" costs context every time and drifts between
agents. Record it once in the data-model collection, ask `dm_*`, and let `dm_validate`
catch the drift (unknown index, missing repository file, Scan creeping in).

### Design record (read + write)

`docs/design/views/<id>.json`:

```json
{
  "id": "schedule-week", "title": "Week schedule", "route": "/app/[slug]/schedule",
  "roles": ["manager"], "flow": "3-from-schedule-to-publish", "requirements": ["SCH-R1"],
  "status": "designed", "artifact_url": "https://claude.ai/artifact/…",
  "artboards": [{ "name": "default", "viewport": 390, "theme": "light", "url": "…" }],
  "brief": { "goal": "…", "primary_action": "Publish", "style": "calm paper", "decoration_budget": 1, "references": [] },
  "components": [{ "name": "AlertBanner", "source": "duotio-ui", "path": "", "props": ["variant"], "states": ["info"], "i18n_keys": ["schedule.title"], "tests": [] }],
  "acceptance": [], "decisions": [], "evidence": []
}
```

`viewport` ∈ 320 | 390 | 1440; `theme` ∈ light | dark; component `source` ∈ `duotio-ui`
(must exist in the registry) | `feature` | `new-duotio-ui` (both need `path`). Status is a
ratchet: each step adds checks that `design_validate` enforces.

### Records (read + write)

Config, one entry per collection (paths relative to the project root):

```json
{
  "records": {
    "claims": {
      "globs": ["docs/company/marketing/claims.json"],
      "schema": "docs/company/marketing/schema/claims.schema.json",
      "id_field": "id",
      "write_file": "docs/company/marketing/claims.json"
    },
    "leads": {
      "globs": ["docs/company/leads/*.json"],
      "schema": "docs/company/leads/schema/lead.schema.json"
    }
  }
}
```

File: `{"meta": {...}, "records": [ {...}, ... ]}` (a bare array is accepted when reading).
Schema: JSON Schema 2020-12 for **one record**; `format: date` / `date-time` are checked.
Whether extra fields are allowed is the schema's decision (`additionalProperties`), not the
server's. When `globs` match several files, `write_file` is the default append target and
`rec_upsert(..., file=...)` picks another (a new path is created). `spec_index` writes an
`INDEX.md` into every directory holding records: one section per collection with the
status distribution when records carry a `status` field.

### Document status

A markdown document may start with YAML front matter:

```yaml
---
status: current        # current | superseded | historical | draft (configurable)
topic: hosting
owner: platform
supersedes:
  - docs/OLD.md
superseded_by: docs/NEW.md
verified_at: 2026-10-11
title: Optional override of the first H1
---
```

`docs_search` / `docs_list` hide `docs.default_hidden_statuses` by default and report how many
files they hid, so retired documents stay in the repo (and in `docs/archive/`) without
polluting answers. `docs_check` audits the corpus; `docs_supersede` retires a document and moves
it to the archive with `git mv` (plain rename outside git).

### Verification

`flow_verify` and `req_verify` answer "does the evidence still point at the code?". A reference
is `path:line[-line]` (flow `evidence`, requirement `text`) and may be sharpened with
`anchors: [{file, symbol, line}]` on a flow step or requirement. Paths are resolved under
`docs.evidence_roots` (bare file names and path suffixes are looked up there). Statuses:

| Status | Meaning |
|---|---|
| `ok` | File found, line in range, symbol (if any) within ±5 lines of `line`. Without a symbol only file and range are checked (`unanchored_ok`). |
| `moved` | Symbol exists elsewhere in the file (`new_line` = nearest). `fix=true` rewrites the reference and the anchor line, shifting a range as a whole. |
| `symbol_missing` / `file_missing` / `line_out_of_range` | Evidence is broken. |
| `ambiguous` | Several candidate files (`candidates`), e.g. the same relative path under two roots. |
| `wrong_root` | The path exists but outside every evidence root (e.g. a decommissioned tree). |
| `invalid_anchor` | Anchor without `file`. |

Flows may also carry a flow-level `async: [{n, runtime, action, evidence}]` list
(`runtime` ∈ `nest|lambda|stepfn|sns|sqs|eventbridge`) for work that happens outside the
request; its evidence is verified too. Markdown flows and requirements are skipped.

### Non-destructive by design

- Markdown is **never rewritten**. Writers (`req_upsert`, `flow_upsert`,
  `spec_import_markdown`, `req_link`, `dm_upsert_*`, `design_upsert`, `rec_upsert`) only
  touch JSON collection files and the sidecar.
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
