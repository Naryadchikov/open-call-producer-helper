# Open-Call Producer Helper

Offline helper used by the Open-Call Producer Grok Bot to research photography open calls, prepare approved submission copies, and independently validate draft packages.

## What it does

- Models source-backed open-call requirements.
- Assesses known requirements without inventing missing facts.
- Builds a read-only preparation plan.
- Creates JPEG submission copies from user-approved finished JPEG/PNG files.
- Preserves aspect ratio, never crops or upscales, strips personal metadata, and embeds sRGB.
- Validates the actual files and ZIP after preparation.
- Keeps every output in `DRAFT_AWAITING_HUMAN_REVIEW`.

It does **not** submit forms, accept terms, pay fees, publish artwork, send email, or alter originals.

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/Naryadchikov/open-call-producer-helper.git
cd open-call-producer-helper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
git clone https://github.com/Naryadchikov/open-call-producer-helper.git
cd open-call-producer-helper
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Quick fictional smoke test

```bash
python -m ocp demo --out workspaces/demo
python -m ocp assess --workspace workspaces/demo
python -m ocp prepare --workspace workspaces/demo --name draft-001
python -m ocp validate --workspace workspaces/demo --package output/draft-001
```

For current real-call workflows, use `assess-call`, `plan-package`, `prepare-draft`, and `validate-draft`. Preparation requires an explicit decisions record bound to the current plan fingerprint.

## Privacy

Keep real work in `workspaces/`, which is gitignored. Do not commit artwork, personal profiles, source snapshots, approvals, credentials, application history, or generated submission packages.

This public repository contains only generic helper code, schemas, and fictional examples. It is intentionally independent of the maintainer's private development repository.

## License

MIT. Third-party dependencies keep their own licenses.
