---
name: assess-fit
description: Reject known mismatches and separate supported eligibility checks from unresolved artist or organiser facts.
---

Read the sourced opportunity and minimal factual profile. Residence is not
nationality or citizenship. Do not infer age, identity, location or credentials
from a name, image, employer or language. Ask only for fields the rules require.

Compare hard conditions before writing an application. Explain a failed rule
with its source.

When a **schema_version 2** opportunity is available, prefer the read-only helper:

```bash
python -m ocp assess-call --opportunity path/to/opportunity.json \
  [--applicant path/to/minimal-facts.json] \
  [--workspace workspaces/CALL_ID] \
  [--now 2026-09-15T12:00:00+00:00]
```

`assess-call` works with the opportunity alone (no artwork, bio, fee budget or
application). Exit `0` = analysis ran without a known hard mismatch (may still be
incomplete); `2` = known hard mismatch; `3` = incomplete dominant; `1` = tool/input
error. `eligibility_decision` is never `eligible` / `fully_eligible`.

For legacy **schema_version 1** workspaces with profile + application:

```bash
python -m ocp assess --workspace workspaces/CALL_ID
```

Do not claim the Python checks cover all possible eligibility, fees or rights.
No currency conversion is implemented. Date-only deadlines do not prove exact
expiry. Organiser "unlimited" image counts are not the helper's INTERNAL_CAPACITY.

Label thematic suitability as an interpretation, not a measured probability of
acceptance. Recommend only approved finished images; the user decides selection.
Flag problematic rights clauses and unresolved costs without providing legal
clearance. Entry fees are not the same as total participation costs.

Known hard mismatch (demonstrated applicant/budget/bound failure or verified
passed exact deadline) -> blocked / known_mismatch. Untrustworthy evidence
(changed snapshot, demo-origin on a real call, conflicting extracted fields) ->
incomplete / insufficient_facts with recapture/review — not applicant rejection.
Missing information -> insufficient_facts / provisional_partial. Do not pressure
the user to apply merely because a deadline is near.

After a usable `assess-call` result and explicit artwork approval, a separate
read-only `plan-package` step can list automatic vs manual vs unresolved
preparation actions for schema_version 2. It reuses the assess-call trust layer
(do not plan production-ready transforms on incomplete/stale/expired calls).
Unresolved required mechanics (e.g. portal max bytes) → `incomplete`, not
`plan_ready`. That plan is not eligibility and does not prepare files.

