# Changelog

## 0.5.0 — 2026-10-11

- **Document status**: front matter (`status`, `topic`, `owner`, `supersedes`, `superseded_by`,
  `verified_at`, `title`). `docs_search` / `docs_list` gain `status`, `topic`, `include_stale`;
  superseded and historical files are hidden by default and counted in `hidden_files`.
- **Chunking**: heading breadcrumb (`path`), soft size limit (`docs.max_chunk_chars`, split on
  paragraphs), front matter skipped; `docs_get(heading)` still returns the whole section.
- **New tools** (40 → 45): `docs_check`, `docs_upsert`, `docs_supersede`, `flow_verify`,
  `req_verify`.
- Evidence anchors (`anchors: [{file, symbol, line}]`) on flow steps and requirements; flow-level
  `async` list; `flows.require_screen_role` makes `spec_validate` warn on empty `screen`/`role`.
- `flow_upsert` accepts anchors on steps and `async_steps`.
- Config: `docs.statuses`, `default_hidden_statuses`, `archive_dir`, `require_front_matter`,
  `stale_patterns`, `evidence_roots`, `max_chunk_chars` (validated at load).
- Index cache version 4 (rebuilds once).

## 0.4.0 — 2026-10-11

- **Records**: generic, schema-validated JSON collections configured under `records.<name>`
  (`globs`, `schema`, `id_field`, `write_file`). New tools `rec_collections`, `rec_list`,
  `rec_get`, `rec_upsert`, `rec_validate`, `rec_aggregate` (34 → 40 tools).
- `rec_upsert` validates against the collection's JSON Schema 2020-12 (incl. `date` /
  `date-time` formats) before writing and refuses with `json_path`-addressed errors.
- `spec_index` writes an `INDEX.md` per directory holding records (status distribution per
  collection); `spec_config().counts.records` lists per-collection counts.
- New dependency: `jsonschema>=4.18`.

## 0.3.1 — 2026-10-11

- Frontend tokens: every `--token` declaration on a line is collected (not only the first).

## 0.3.0 — 2026-10-11

- UI component registry (`ui_*`) and design records (`design_*`).

## 0.2.0 — 2026-10-11

- Data model (`dm_*`): tables, GSIs, pseudo-relations, access patterns, lint.

## 0.1.0 — 2026-10-11

- Docs search, requirements, flows, coverage, validation, `INDEX.md` generation.
