# Changelog

## 0.7.0 — 2026-10-12

Retrieval tuning for `docs_semantic_search` / `docs_ask`, driven by a 24-question evaluation on a
real 4 182-item corpus. Every new key is optional; without configuration only the default
generator prompt changed (see below). Tool count unchanged (49).


Measured on the Duotio corpus (4 182 embedded items, 24 questions, `mistral-small3.2:24b`;
n is small, run-to-run noise is about +-2 questions):

| Configuration | Correct (automatic regex check) | Median latency |
|---|---|---|
| 0.6.0, no tuning | 18 / 24 | 28.8 s |
| 0.7.0, all tuning (status boost 0.85, record 1.1, flow 1.05, reserve 2+1, window 30) | 20 / 24 | 47.2 s |
| 0.7.0, final (record 1.1, reserve 1+1, window 400, structured routing) | 20 / 23 + 1 timeout; 22 / 24 after re-running the 3 flipped questions | 43.8 s |

Retrieval-only ablation (expected source in top 3 / anywhere in top-K, 20 answerable questions):
no tuning 14 / 15; `status_boost.draft` 0.97 -> 13 / 15, 0.95 -> 12 / 15, 0.85 -> 11 / 14 (drafts were the
only source for several facts); `kind_boost.record 1.1` 14 / 16; record 1.1 + reserve 1+1 window 400
14 / 16. Latency grows because list questions send up to 12 sources. Two of the remaining
"incorrect" answers were correct in substance but missed the evaluator's regex (an answer that
restates the question, an answer without a function name).

- **`ollama.rank`**: `status_boost` (per document status, `unknown` = no front-matter status when
  `docs.require_front_matter` is on) and `kind_boost` (`record|flow|requirement|doc`) multiply the
  hybrid score for ORDERING only (`rank_score`). `min_score` and the reported `score` always use the
  score before boosting, so tuning the order never changes the refusal gate.
- **`ollama.reserve`** + `route_terms` / `flow_terms`: when the question names routes (or flows),
  the best `record` (flow-step) hits found inside `reserve_window` (default 30) are guaranteed
  slots in the result (`reserved: true`), replacing the weakest other hit.
- **`reserve_window`** (default 30): how deep in the ranking a reserved candidate may sit. Records
  written in another language than the question rank far below 30; 400 reached them on the
  evaluation corpus.
- **Adaptive `top_k`**: list-type questions (`list_terms`) use `max(top_k, min(top_k_list, 14))`;
  `max_context_chars` (default 14000) drops the weakest sources, never reserved or structured ones
  (`context_truncated`).
- **Generator prompt** (new default, override with `system_prompt` / `system_prompt_file`): answer
  every part of the question, append "Brak w dokumentacji: …" only for parts without a source, state
  explicit absences ("0 trafień", "nie istnieje") as a fact instead of refusing, and never list
  things the sources describe as existing-but-uncalled/disabled as working.
- **Post-check**: `full_refusal` (nothing but "Brak w dokumentacji" lines), `partial_gap` (content AND a "Brak w dokumentacji" line) and
  `consistency_warning` (the gap line is about the same entity as the question). A partial gap no
  longer forces `confidence: low`.
- **Structured routing**: `route_hint: "structured"` + `suggested_tool` (`req_list`,
  `rec_list`, `rec_aggregate` with ready arguments) for status / count / role questions
  (`structured_patterns`, defaults included; patterns whose collection does not exist are skipped).
  With `route_structured: true` the server runs the tool read-only and feeds its result to the
  generator as source `[1]` (`structured_result`, at most 50 rows).

## 0.6.0 — 2026-10-11

- **Optional local-RAG layer over Ollama** (45 → 49 tools): `ollama_status`, `docs_reindex`,
  `docs_semantic_search`, `docs_ask`. Enabled by an `ollama` config section; without it the
  tools answer `ollama_not_configured` and every 0.5.0 tool behaves as before.
- `docs_ask`: hybrid retrieval (saturated BM25 + embedding cosine) -> numbered sources ->
  Ollama chat answer with `[n]` citations. Returns `grounded`, `confidence`, `citations_valid`,
  `uncited_sentences`, per-source `evidence_ok` (file + symbol + line checked against the code
  for flow / requirement sources). `strict=true` refuses below `min_score` without calling the
  model (`insufficient_evidence` + nearest hits).
- Corpora: heading chunks of non-hidden documents, flow steps and async steps, requirements
  (text + acceptance), records of configured collections (`records:<name>`). Superseded and
  historical documents stay BM25-only (shown by `include_stale`).
- Incremental sqlite embedding cache (`cache_path`): content-hash reuse, orphan removal, full
  rebuild on `embed_model` change, lazy refresh of small backlogs (`lazy_embed_limit`).
- Structured errors: `ollama_unavailable` (retryable, hint "start Ollama"), `model_missing`
  (hint `ollama pull <model>`), `ollama_not_configured`, `insufficient_evidence`.
- New dependency `httpx`; optional extra `fast` (`numpy`) for vector scans.
- Measured on a 4 182-item real corpus: indexing 142 s (bge-m3), retrieval 140 ms pure Python /
  17 ms with numpy, 10 000 x 1 024 cosine scan 241 ms / 3 ms.

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
