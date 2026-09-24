# Data contract

Version 1 remains the package-preparation contract. Version 2 adds a richer
read-only requirements model for `assess-call`. Do not silently reinterpret v1
as v2.

---

## Data contract, version 1

A workspace contains three strict JSON files, original image copies and a source
snapshot. Use `ocp/schemas/*.schema.json` as the authority, and `examples/` only as
fictional examples. The helper rejects unknown keys, duplicate JSON keys and
unsupported paths. A custom input authoring UI is not implemented.

## profile.json

- `schema_version`: 1.
- `artist_name`: user-approved public/artist name, not inferred identity.
- `residence_country`: two-letter country code or null. Not citizenship.
- `approved_bio`: factual biography supplied or explicitly approved by the user.
- `fee_budget`: nonnegative amount and explicit three-letter currency.

Do not collect birth dates, passports, personal addresses, phone numbers or email
unless a real requirement creates a separate, authorised need. They have no field
in this version. Never insert user-specific values into public examples.

## opportunity.json

`demo_only` must be false for actual calls. Fictional data must never be converted
into an actual opportunity just by toggling this flag. Source fields include
original HTTPS URL, UTC-offset retrieval time, UTF-8 snapshot path, SHA-256 and a
record that complete instructions were captured. The snapshot is private job data.
A pasted email or screenshot alone may not contain the complete rules.

Each fact is `{ "value": ..., "evidence": { "quote": "...", "locator": "..." } }`.
A genuinely unknown fact is `{ "value": null, "evidence": null }`. Quotes must be
literal substrings of the snapshot. The check does not understand the quotation's
meaning or independently verify that a normalised number is accurate.

| Fact | Normalised meaning |
|---|---|
| residency_allowed | Country-code array; `*` only for explicitly unrestricted residency. |
| media_allowed | Accepted medium labels; selected artwork must use one of them. |
| deadline | ISO timestamp with explicit UTC offset, accounting for date-specific daylight saving. |
| fee_amount / fee_currency | Stated total entry fee for this selection, not per-image fee unless that is the actual total. |
| min_images / max_images | Accepted count of image files. Unsupported series/group counting must be flagged. |
| allowed_formats | Accepted actual file formats; current helper requires JPEG among them. |
| min_long_edge_px / max_long_edge_px | Bounds on the longer side; other geometry rules remain unresolved. |
| max_file_bytes | Decimal bytes per image when exact bytes are verified. Do not silently convert MB/MiB wording without verifying the organiser's intent. (Optional UI wording labels belong in the v2 file-byte-bounds model only; v1 rejects undeclared fields.) |
| statement_max_words / bio_max_words | Maximum words; helper counts whitespace-separated tokens. Portal counting may differ. |
| filename_rule | `any` only when filenames are explicitly unrestricted. Other values need an adapter. |
| rights_clause | Short faithful summary tied to the original excerpt; always requires human legal/rights review. |

Unknown values do not mean unrestricted. This strict starter will stop on calls
with unspecified limits rather than invent values. Supporting explicit
`not_applicable` or sourced `no_limit` is a future schema change with tests, not a
reason to enter arbitrary huge values now.

`unresolved_requirements` must contain all material conditions outside this
model: age, citizenship, qualifications, status, exclusivity, AI policy, original
creation dates, shipping/production costs, licences, exact dimensions, file metadata,
DPI, image anonymity, custom naming, number of series, required PDF CV, portal fields,
and any conflicting instructions. An empty list is a reviewed assertion, not
proof that nothing was missed.

Real source captures older than 72 hours, or materially future-dated, require
rechecking. This is our conservative workflow policy, not a contest rule. The
organiser's actual deadline still governs. Always recheck before manual submission.

## application.json

`statement` is the current draft—not automatically an approved artist statement.
The package always preserves draft state. `untagged_colour_policy` is `reject`
or explicitly authorised `assume_srgb`. Do not infer it from a filename.

Each approved finished artwork has a safe unique ID, title, year, medium, relative
source path, credit and `approved_for_submission_copy`. This permission is supplied
by the user; the bot must not set it on its own. Source paths are confined to the
workspace, with no symlinks or traversal. IDs and source paths must be unique.

No artistic arrangement or finishing instructions are part of this contract.
Artwork order is user-approved submission order; it does not change pixels inside
an artwork. A finished composite is one opaque image like any other submitted file.

## Outputs and states

A successful run creates submission files, review artifacts, a manifest and a ZIP
under a new `output/<name>/`. Names are never reused. Original inputs are not altered.
All drafts require human review even after mechanical checks pass.

The manifest includes source hashes, input hashes, measured derivative properties,
colour-handling notes and output hashes. It is unsigned: modification by someone
who also rewrites the manifest is not cryptographically prevented.

The private review contains source-derived requirements. Only image/text submission
files enter `submission.zip`. Do not send the ZIP without inspecting its contents
and confirming the actual organiser's upload requirements.


---

## Opportunity schema_version 2

Authority: `ocp/schemas/opportunity.v2.schema.json`. Used by `python -m ocp assess-call`.
v1 `assess` / `prepare` / `validate` **reject** v2 workspaces with an explicit error;
they are not silently downgraded.

### Sources

- Primary `source` block plus optional `additional_sources[]`.
- Each source has `source_id`, `kind`, optional `label`, `url`, `captured_at` (with
  UTC offset), `snapshot_path`, `snapshot_sha256`, `complete_rules_captured`.
- Requirement `evidence` must include `source_id` + literal `quote` + `locator`.
  Quotes must be substrings of **that** source's snapshot. Never invent quotations
  for absence of information.

### Knowledge status vs helper support

Each modelled requirement carries both:

| Field | Values | Meaning |
|---|---|---|
| `knowledge_status` | `known`, `explicitly_unrestricted`, `not_required`, `not_stated`, `inaccessible`, `conflicting` | What the organiser sources establish |
| `helper_support` | `supported`, `manual`, `unsupported` | What this helper version can enforce |

Silence or inaccessible portal fields are **not** optional/unrestricted.

### Deadline

- `deadline_date` (YYYY-MM-DD) with evidence when a calendar date is stated.
- `deadline_time` / `deadline_timezone` independently nullable.
- `deadline_datetime` (full ISO with offset) **only** when time and offset are known.
- Parse/validate the instant before setting `exact_expiry_known`. Cross-check
  datetime vs date/time/timezone when both are supplied; contradictions are
  review/input errors, not open/closed decisions.
- Never invent midnight or claim exact expiry from a date alone.

### Fees (`fee_schedule`)

- `base_fee`, `images_included`, `additional_image_fee`, `optional_charges[]`
  (`id`, `amount`, `description`, `selected` default false, optional `condition`).
- Optional-charge `condition` is machine-readable only — never inferred from
  `description`. Supported: `complimentary_at_or_above_image_count` with
  `threshold` (amount applies when N < threshold; selected charge is $0 at/above);
  `unresolved` (selected charge makes the total non-calculable / manual).
  Unsupported or unresolved selected conditions must not become hard budget mismatches.
- `currency_raw` (e.g. `"$"`) separate from `currency_code` (ISO 4217 or null).
- Fee helper: for N images, base covers `images_included`, then
  `additional_image_fee * max(0, N - included)`; optional charges only when the
  applicant explicitly selects them (organiser `selected` is not user permission;
  an explicit empty list excludes all optionals).
- `knowledge_status=not_required` for fee or deadline establishes that no such
  requirement applies; specialized affordability/deadline checks are skipped.
- Arithmetic uses decimal-safe money values (cent quantize). Capability flags are
  `fee_total_calculable` and `currency_code_known` — never an affirmative budget
  approval from the fee alone. Affordability is reported only after comparing a
  complete applicant budget to the resolved total; a budget match is not payment
  permission.
- Unknown currency or counting units → schedule preserved; affordability stays
  incomplete.

### Image counts (`image_count`)

- `rule`: `limited` | `unlimited` | `not_stated` | `inaccessible` | `conflicting`.
- When `limited`: evaluate each established `min` / `max` independently (max-only
  or min-only are valid). Do not invent the unknown opposite bound.
- When `unlimited`: organiser has no maximum; an explicit `min` still applies.
- Conflicting extracted ranges are source-review problems, not applicant mismatches.
- Helper `INTERNAL_CAPACITY = 30` is a **tool** limit, never encoded as organiser max.
  Selecting more than 30 may still be an organiser match, but always yields a separate
  capacity/review limitation — never applicant ineligibility — for both `limited` and
  `unlimited` rules. Empty `rule=limited` with null min and max is source-review, not
  a positive match.
- Keep **entry / submission image count** (call-page rules) distinct from **upload-widget
  file count** (e.g. “max 20 files” or one ZIP). A call-page unlimited entry count is not
  automatically contradicted by an upload-widget file limit. Owner choice of ≤20 images is
  a **working constraint**, not organiser evidence — do not rewrite organiser
  `image_count.max` to 20 from the widget alone.
- Preserve whether an exhibition-history restriction applies to the **submitted work** or to
  the **applicant**. For A. Smith's work-specific clause, do not reject an artist because
  different work previously exhibited (that selected-work reading is not a universal policy).

### Other requirements

Residency, media, formats, long-edge bounds, file bytes, statement/bio word limits,
filename rule and rights clause mirror v1 facts with the richer status model.
`unresolved_requirements` still lists material conditions outside the model.

### assess-call outputs

Read-only JSON. `status` examples: `analysis_complete`, `blocked`, `incomplete`.
`eligibility_decision` is never `eligible` / `fully_eligible`; use
`insufficient_facts`, `known_mismatch`, or `provisional_partial`. Successful
analysis is not an eligibility badge.

`known_mismatch` / exit `2` is reserved for demonstrated applicant mismatches
(residency, media, image-count bounds, affordability after a proper compare) or a
verified passed exact deadline. Snapshot hash failures, demo-origin misuse,
conflicting extracted ranges, and other untrustworthy evidence yield
`incomplete` / `insufficient_facts` (exit `3`) with a recapture/review next action
— never applicant ineligibility.

Established facts retain `evidence` (`source_id`, `quote`, `locator`). The report
also includes `source_metadata` (urls, capture times, hashes, kind — not snapshot
text). Exit codes: `0` analysis with no known hard mismatch; `2` known hard
mismatch; `3` incomplete / untrustworthy evidence dominant; `1` tool/input error.

Real calls (`demo_only=false`) must not establish facts from `kind=demo` sources,
and `original_page` requires an https URL. `user_supplied_copy` may omit a URL.

Authoritative source kinds for establishing organiser requirements:
`original_page`, `user_supplied_copy`, `application_instructions`,
`organiser_listing`, `organiser_services`, and `demo` (demo only when
`demo_only=true`). `secondary_aggregator`, `access_blocker_evidence`, and `other`
are contextual/non-authoritative — useful for review notes, never for hard
eligibility/mismatch conclusions alone.


---

## Artwork manifest + plan-package (schema_version 2 bridge)

Authority: `ocp/schemas/artwork_manifest.schema.json` (input) and
`ocp/schemas/preparation_plan.schema.json` (output shape documentation).
Command: `python -m ocp plan-package`.

### artwork_manifest.json

Minimal approved-artwork list for planning only. Fields:

| Field | Meaning |
|---|---|
| `schema_version` | 1 |
| `artworks[].id` | Stable artwork id (slug) |
| `artworks[].path` | Workspace-relative source path |
| `artworks[].submission_order` | Explicit approved submission order (1..N) |
| `artworks[].title` / `year` / `medium` | Optional when supplied |
| `artworks[].approved_for_submission_copy` | Must be explicitly `true`; otherwise plan stops |

The plan does **not** select works. Absent or false approval → `blocked`.

### Applicant facts for planning

Same minimal keys as `assess-call`, plus mechanical `first_name` / `last_name`
when an explicitly supported filename adapter (A. Smith-style
`1FirstName_LastName.jpg`) requires them. Missing required name fields block
filename planning (`status=blocked`, exit `2`).

### Preparation plan classifications

Each organiser requirement is classified as one of:

- `automatic_supported` — helper can later perform a constrained action
- `manual_check` — human must resolve (watermark, rights, fees, theme/AI/prior exhibition)
- `blocked_unsupported` — required transform outside helper scope
- `unresolved_source` — organiser fact inaccessible/not stated (e.g. portal max bytes)
- `not_applicable` — not required / unrestricted

Automatic actions record: source requirement/evidence, intended operation,
affected artworks, whether pixels/metadata/filename/packaging change, safety
constraints, and later validation. Organiser rules are distinguished from tool
limits (e.g. `INTERNAL_CAPACITY=30`).

### Exit codes (`plan-package`)

| Code | Meaning |
|---|---|
| 0 | `plan_ready` — no unresolved required mechanics; prepare-blocking manuals (fee/rights/watermark/colour/deadline/media) keep `ready_for_prepare=false`; informational manuals do not |
| 2 | `blocked` — safety / assess-call known_mismatch / selection-count / unsupported transform gates |
| 3 | `incomplete` — unresolved organiser mechanics (e.g. portal `max_file_bytes`) or assess-call trust/freshness/integrity gaps |
| 1 | Tool/input/schema error |

`plan-package` bridges the hardened `assess-call` trust layer (snapshot integrity,
freshness, excerpt verification, exact-deadline expiry) rather than reinterpreting
raw v2 opportunity data alone. Unresolved upload limits and other required
mechanics yield `incomplete` / `ready_for_prepare=false` — not `plan_ready`.
`submission_order` must be contiguous `1..N`.

`plan-package` never calls `prepare`, never modifies sources, and must not be
used to silently mix v1/v2 prepare behaviour. v1 `prepare` still rejects v2
workspaces.


---

## prepare-draft / validate-draft (schema_version 2 execution)

Commands: `python -m ocp prepare-draft` and `python -m ocp validate-draft`.
Authority for decisions: `ocp/schemas/prepare_decisions.schema.json`.

### Execution fingerprint (plan_fingerprint)

A SHA-256 over the stable **execution identity** used for execute approval.
**Includes:** opportunity id, source snapshot hashes, material requirements
(knowledge_status + value-like fields), artwork ids/paths/submission_order/source
sha256 **plus title/year/medium**, automatic_action ids + operations + key params
(edge/bytes/ppi/filenames/adapter), filename adapter, **packaged statement and
biography text** (exact strings that prepare-draft would write when the call
requires/exports them), and **processing.untagged_colour_policy**.
**Excludes:** volatile timestamps such as `assessed_at` / wall-clock /
`approved_at`.

Changed artwork bytes, artwork metadata (title/year/medium), statement or bio
text, colour-processing policy, or material rules produce a new fingerprint and
invalidate execute approval. Do not silently renew approval when content changes.

### Decisions record (private, local JSON)

Path passed via `--decisions`. Distinguishes:

1. **artwork_selection** — approved ids in submission order
2. **processing.untagged_colour_policy** — `assume_srgb` | `reject` | null
   (null only when no untagged sources; never silently assumed)
3. **manual_acknowledgements** — authorize prepare for prepare-blocking manuals
   when tests need them; MUST NOT invent organiser facts, force-pass, invent
   upload limits, or rewrite `knowledge_status` to unsupported `not_required`.
   Prefer fixtures that use honest `knowledge_status=not_required` for
   fee/rights/deadline/media so `ready_for_prepare=true`.
4. **execute_approval** — approval to execute the specific plan fingerprint

Synthetic tests should set `demo_only` / `fictional_test_record: true`.

### Text emission rules

- `artist-statement.txt` / `artist-bio.txt`: only when the call establishes a
  known word limit (`knowledge_status=known`) and the applicant supplied approved
  text — never invent credentials or silently rewrite factual text.
- `image-list.txt`: when artworks have titles (minimal credits/list).
- If no text is required, the ZIP may be images-only (+ image-list when titles exist).

### Output package (manifest schema_version 2)

New `output/<name>/` only (refuse if exists). Stage under `.preparing-*`; delete
on failure. Structure:

- `submission/` — images + required text only
- `review/` — contact-sheet.html, plan.json, assessment summary, report.md
- `manifest.json` — state `DRAFT_AWAITING_HUMAN_REVIEW`, `plan_fingerprint`,
  input/artwork hashes, image rows, file hashes
- `submission.zip` — submission membership only

CLI JSON always reports `human_review_required=true`, `submitted=false`.

### Exit codes

| Code | prepare-draft / validate-draft |
|---|---|
| 0 | prepared / validated_draft |
| 1 | tool/input error |
| 2 | blocked/refused (plan blockers, unsupported ops, tamper, colour reject) |
| 3 | incomplete / missing execute approval / unresolved mechanics / colour undecided |

### File byte bounds (organiser vs tool vs effective)

`max_file_bytes` models the **organiser-declared** per-file upload limit (`value`
exact decimal bytes when verified; optional `declared_label` for UI wording). Schema
maximum on `value` is only a modelling ceiling for large organiser declarations (e.g.
268435456) — it is not the helper encode ceiling and not permission to treat unknown
limits as unrestricted.

Helper `TOOL_MAX_FILE_BYTES` (64 MiB) in `ocp/imaging.py` is the processing/output
ceiling. When an exact organiser byte limit is known, the **effective** encode and
validate bound is `min(organiser_bytes, TOOL_MAX_FILE_BYTES)`. Reports and plan
`detail` must distinguish:

- organiser declared (`value` / `declared_label` / evidence)
- tool ceiling (`TOOL_MAX_FILE_BYTES`)
- effective bound + which bound is controlling

Never record the tool ceiling as the organiser's rule. Never invent MB/MiB→byte
conversions. Label-only or unknown limits stay unresolved for exact-byte planning
(not silently unrestricted).

When organiser `max_file_bytes` is `not_required` / `explicitly_unrestricted`,
encoding uses the tool ceiling only — never presented as an organiser upload limit.
When `min_long_edge_px` is `not_required`, min=1.

