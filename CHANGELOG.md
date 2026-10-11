# Changelog

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
