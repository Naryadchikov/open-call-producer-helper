# Architecture

## Deliberately small

The bot performs source gathering, factual extraction, drafting and conversation.
A deterministic Python helper assesses structured inputs, transforms approved
submission copies and validates actual outputs. Human review handles full rules,
rights, artistic judgment and any external submission. None of those roles is
collapsed into an unsupported blanket "ready" claim.

```text
Original organiser rules ─┐
Minimal artist facts ─────┼─> Bot/user normalisation ─> Strict JSON contracts
Approved finished images ┘                                 │
                                              Offline assessment
                                                           │
                                              New draft preparation
                                                           │
                                           Actual file / ZIP validation
                                                           │
                                      Human review, then MANUAL submission
```

## Modules

- `io.py`: bounded JSON, schema validation, hashes, timestamps and safe paths.
- `assessment.py`: v1 workspace checks and conservative outcome states.
- `call_assessment.py`: read-only schema_version 2 opportunity analysis (`assess-call`); no package prep.
- `package_plan.py`: read-only v2 preparation plan (`plan-package`); never executes prepare or modifies images.
- `decisions.py`: stable plan fingerprint + private prepare-decisions JSON binding.
- `draft_v2.py`: v2 `prepare-draft` / `validate-draft` execution against the plan + decisions.
- `imaging.py`: orientation/colour handling, no-upscale JPEG copies, measurement.
- `packaging.py`: staged new-run creation, evidence, proof and submission-only ZIP;
  independently checks current inputs and actual files.
- `demo.py`: fictional workspaces and generated geometric test images.
- `cli.py`: stable JSON output and meaningful exit codes for humans or bots.
- `scripts/export_public_kit.py`: explicit public-content selection, not publishing.

Core execution has no model API, browser, account login, network dependency,
telemetry or credential requirement. Installing dependencies is a separate action.

## Publication seam

A private development repo owns implementation and tests. An allowlisted export
contains only generic administrative instructions/helpers and fictional samples.
A future native Grok template points to a reviewed accessible helper distribution.
Never give recipients a dependency on the maintainer's private repository.

Bot instructions and skill Markdown are versioned source material. No custom
Grok serialization/import protocol is invented. App installation and publication
remain manual until a documented official integration is deliberately added.

## Decisions

- No frontend until the CLI-backed workflow succeeds on real calls.
- No discovery until the supplied-opportunity path works.
- Opportunity v2 enables structured real-call modelling; `plan-package` plans v2 preparation read-only;
  `prepare-draft` / `validate-draft` execute a bounded plan when decisions match the plan fingerprint.
  v1 `prepare` must not silently mix v2.
- No shared knowledge base, vector storage or agent swarm.
- No pipeline connection to private photographic editing software.
- No automated send/pay/submit, even behind a reassuring prompt.
- No live CI claims: a workflow file is not evidence that it ran on GitHub.
