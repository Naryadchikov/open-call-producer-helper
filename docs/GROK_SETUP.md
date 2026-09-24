# Grok Bot setup

This repository is the public helper used by Open-Call Producer. It contains generic code and fictional examples only.

## Install the helper

Clone this repository in the bot's authorised workspace and install the pinned dependencies:

```bash
git clone https://github.com/Naryadchikov/open-call-producer-helper.git
cd open-call-producer-helper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the fictional smoke test from README.md before handling personal work.

## Operating boundary

Use the helper only for the current photography call and files the user deliberately supplies. Start with read-only research. Ask for the minimum facts needed for the call.

The helper supports:
- `assess-call` for sourced opportunity assessment;
- `plan-package` for read-only preparation planning;
- `prepare-draft` after explicit approval;
- `validate-draft` for independent validation of the generated package.

If the helper cannot be installed or verified, remain ANALYSIS_ONLY. Do not claim that images or ZIPs were measured or validated.

Never submit forms, pay fees, publish artwork, accept organiser terms, send email, or access unrelated files. Do not upload a user's full archive or creative project files. Treat finished artwork as opaque: do not crop, retouch, restyle, upscale or reconstruct it.

## Native Grok template

The native template should include the Open-Call Producer operating instructions and the task skills for requirement extraction, fit assessment, draft preparation and package verification.

Before public sharing, inspect the template details and ensure it contains no personal memories, real artwork, application history, credentials, routines or private repository links.

A fresh user should be able to use this public helper without access to any maintainer-only repository.
