# Demonstration plan

The bundled call, artist, dates and geometric image fixtures are fictional.
Label them prominently. They demonstrate mechanics, not an actual open-call
submission or successful Grok installation.

## 60–90 second story

0–10 seconds: "This bot prepares photography submissions, but first checks whether
applying makes sense. It does not change how the artist makes the work."

10–25 seconds: Run the ineligible fixture. Show the source residency restriction,
the different supplied residence, and the blocked result. No application is made.

25–55 seconds: Run the ready fixture. Show the original rules and approved files,
then actual created JPEGs, measured dimensions/bytes, the statement and image list.
Open the visual proof rather than showing only a prompt.

55–75 seconds: Validate the real package and ZIP. Show that source originals are
unchanged, review notes are outside the ZIP, and all outputs remain drafts.

75–90 seconds: State the human checks: complete rules, artist facts, rights,
visual quality and actual portal requirements. No submission/payment follows.

## Additional useful negative cases

Unknown deadline -> needs_review, not guessed date. Changed source -> blocked.
Too-small image -> no upscale. Impossible file-size rule -> stop at quality floor.
Missing helper -> ANALYSIS_ONLY, no claim of measured output.

## Evidence to save privately

Exact code revision, interpreter/dependencies, source/input hashes, observed
commands/exit codes, output manifest, test report and a screen recording of actual
bot interaction. Use fictional inputs for a public recording; never show private
browser tabs, credentials, image archives, personal profiles or workspace paths.

Do not call a terminal-only run a Grok Bot demo. Record native bot execution when
M1 is actually complete. No time-saved or acceptance-rate claims without measurement.
