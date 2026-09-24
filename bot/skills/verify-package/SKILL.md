---
name: verify-package
description: Measure the actual output files and ZIP, preserve evidence and require human review.
---

## v1 packages

Run `python -m ocp validate --workspace workspaces/CALL_ID --package output/RUN_ID`
against the actual prepared directory. Respect failures and nonzero exit codes.
Do not substitute a test listing, imagined command result or a code review.

## v2 drafts

Run:

```bash
python -m ocp validate-draft \
  --workspace workspaces/CALL_ID \
  --package output/RUN_ID \
  --opportunity path/to/opportunity.json \
  --artwork-manifest path/to/artwork_manifest.json \
  [--applicant path/to/facts.json] \
  --decisions path/to/prepare-decisions.json
```

Validate **actual saved output** against the current plan fingerprint and inputs:
JPEG format/dims/colour/ICC, required 72 ppi when planned, file sizes, exact
filenames/count/order, required text + word limits, source/input/output hashes,
exact ZIP membership. Changed inputs, tampered files, or unexpected ZIP entries
fail — resolve by correcting inputs or creating a **new** draft, never by
weakening expectations.

Exit `0` = validated_draft; `2` = blocked/refused; `3` = incomplete; `1` = tool/input error.

## Shared review rules

Check file count, identities/order, dimensions, byte sizes, text, source hashes,
manifest hashes and ZIP contents. Open the HTML contact sheet and inspect the
actual submission images. Inspect metadata and colour-handling notes. Do not
claim print calibration or perceptual parity based on these mechanical checks.

Recheck original rules before the user's final submission. Re-prepare a new draft
when source rules, input profile, original files or text change. An unsigned local
manifest establishes integrity relative to inputs, not third-party certification.

Present the exact package path, submitted=false, measured results, source-backed
requirements, unresolved/manual checks and required human decisions. The ZIP is
submission files only; private review notes and originals stay outside it.

An independent human checks complete eligibility, rights, authenticity, artist
facts, text meaning, visual quality and portal-specific requirements. No automatic
submission follows a successful validation.
