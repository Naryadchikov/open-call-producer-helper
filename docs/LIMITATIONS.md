# Limits to preserve in every demonstration

1. The helper does not read websites or PDFs, extract rules, interpret rights or
   verify artist facts. A bot/user prepares the structured inputs. Quote/hash
   matching verifies only local textual consistency. There is no full legal,
   authenticity, scam or eligibility certification.
2. Supported criteria are intentionally narrow. Missing limits and custom naming,
   exact aspect ratios, DPI/metadata rules, age/citizenship/qualifications, special
   series counts, CV PDFs, total costs and portal forms need review/adapters.
3. Only opaque RGB/grayscale JPEG/PNG inputs and JPEG outputs. No RAW/HEIC/TIFF,
   alpha flattening, animation, palette images, CMYK or wide-gamut print guarantee.
   No crop, upscale, retouch, creative sequencing or preset distribution.
4. Sources: at most 64 MB and 40 MP per image; at most 30 artworks. JSON inputs are
   bounded to 2 MB and source snapshots to 1 MB. Encoding may be memory intensive
   on small machines. No streaming large-image/tiled processing.
5. Byte-size fitting tries JPEG quality 95, 90, 85, 80 at fixed resized dimensions
   and no chroma subsampling. It stops below that floor rather than silently
   shrinking dimensions again. A visually unacceptable result still requires
   human rejection even when within the byte limit.
6. Text lengths count whitespace-separated tokens. A portal may count hyphens,
   apostrophes, non-Latin text and punctuation differently. Statement factuality
   and voice are manual checks; the helper never rewrites text.
7. Only direct dependency versions are pinned. Cross-OS/older-Python CI is planned
   in the workflow but not validated until actual runs succeed. Pixel-identical
   exports across platform/library versions are not promised.
8. Public-kit export is not template publication, license selection or an audit.
   The basic pattern scanner is not exhaustive. Native Grok installation and a
   fresh-user/account test are still required.
9. A clean bot on the same platform account is not necessarily a clean computer.
   Do not assume credentials and files are isolated without checking permissions.
10. No real user/workplace data belongs in the repository. No organiser application,
    communication, payment, public publication or competition entry has been made.

11. Opportunity schema_version 2 + `assess-call` model richer deadlines, fees and
    unlimited counts, but do not prepare submission packages, rename files, set PPI,
    fill portals, convert currency or certify eligibility. Date-only deadlines never
    become exact expiry. Helper INTERNAL_CAPACITY (30) is not an organiser maximum.
12. v1 assess/prepare/validate reject v2 opportunities rather than silently
    downgrading. Portal fields blocked by captcha/inaccessible HTML remain
    `inaccessible` / incomplete.

13. `plan-package` produces a read-only preparation plan for schema_version 2 + an
    approved artwork manifest. Filename planning supports only the explicit A. Smith-style
    `1FirstName_LastName.jpg` adapter. PPI is promoted only from verified organiser
    evidence. JPEG encode requires opaque tagged RGB/L; untagged needs an explicit
    colour decision (`assume_srgb`|`reject`). Portal max-file-size that remains
    inaccessible yields `incomplete` / `ready_for_prepare=false`.
14. `prepare-draft` / `validate-draft` execute only supported automatic operations from
    the plan (downscale-only JPEG, optional 72 ppi JFIF density, A. Smith filenames,
    required text). They require a private decisions file bound to a stable plan
    fingerprint (no volatile timestamps). Acknowledgements authorize prepare only —
    they are not organiser evidence and must not invent limits or force-pass.
    Organiser `max_file_bytes` (declared value/label) is separate from helper
    `TOOL_MAX_FILE_BYTES` (64 MiB). Effective encode/validate bound is
    min(organiser, tool) when exact organiser bytes are known; tool ceiling is never
    recorded as the organiser rule. When organiser `max_file_bytes` is not_required,
    encoding uses the tool ceiling only — never an invented organiser upload limit.
    When `min_long_edge_px` is not_required, the tool minimum is 1 px. Output names are
    never reused; failed runs delete temp staging. Every draft remains
    `human_review_required` / `submitted=false`. No submit/pay/portal automation.

