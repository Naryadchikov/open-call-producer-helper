"""Deterministic checks of HUMAN/AGENT-NORMALISED facts, not legal interpretation."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .io import ProducerError, checked_root, load_json, local_path, sha256, utc_time, word_count

FACT_NAMES = (
    "residency_allowed", "media_allowed", "deadline", "fee_amount", "fee_currency",
    "min_images", "max_images", "allowed_formats", "min_long_edge_px",
    "max_long_edge_px", "max_file_bytes", "statement_max_words", "bio_max_words",
    "filename_rule", "rights_clause",
)


def load_workspace(workspace: Path) -> tuple[Path, dict, dict, dict]:
    root = checked_root(workspace)
    # If an opportunity file is present, reject v2 before the v1 triad is required.
    opportunity_path = local_path(root, "opportunity.json", must_exist=False)
    if opportunity_path.is_file():
        preview = load_json(opportunity_path, "opportunity")
        if preview.get("schema_version") != 1:
            raise ProducerError(
                "Workspace opportunity uses schema_version "
                f"{preview.get('schema_version')}. "
                "v1 assess/prepare/validate do not support opportunity v2 in this milestone; "
                "use `python -m ocp assess-call --opportunity ...` for read-only v2 analysis, "
                "or provide a schema_version 1 opportunity for package preparation."
            )
    values = [load_json(local_path(root, f"{name}.json"), name)
              for name in ("profile", "opportunity", "application")]
    ids = [art["id"] for art in values[2]["artworks"]]
    if len(ids) != len(set(ids)):
        raise ProducerError("Artwork identifiers must be unique")
    paths = [art["path"] for art in values[2]["artworks"]]
    if len(paths) != len(set(paths)):
        raise ProducerError("The same artwork file is selected more than once")
    for art in values[2]["artworks"]:
        path = local_path(root, art["path"])
        if path.stat().st_size > 64_000_000:
            raise ProducerError(f"Original exceeds 64 MB input limit: {art['id']}")
    return root, *values


def assess(workspace: Path, *, now: datetime | None = None) -> dict:
    root, profile, call, application = load_workspace(workspace)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    checks: list[dict] = []

    def add(key: str, state: str, message: str) -> None:
        checks.append({"id": key, "status": state, "message": message})

    source = call["source"]
    source_path = local_path(root, source["snapshot_path"])
    if source_path.stat().st_size > 1_000_000:
        raise ProducerError("Source snapshot exceeds 1 MB text limit")
    text = source_path.read_text(encoding="utf-8")
    integrity_ok = sha256(source_path) == source["snapshot_sha256"]
    add("source_integrity", "pass" if integrity_ok else "fail",
        "Snapshot hash matches the supplied record" if integrity_ok else "Source snapshot changed")
    if not source["complete_rules_captured"]:
        add("source_completeness", "review", "Complete rules have not been captured and reviewed")
    if not call["demo_only"]:
        if source["kind"] == "demo" or not source["url"]:
            add("source_origin", "review", "Real opportunities require an original organiser URL")
        age = (now - utc_time(source["captured_at"])).total_seconds()
        if age < -300 or age > 72 * 3600:
            add("source_freshness", "review", "Recheck original terms: source is future-dated or older than 72 hours")
    else:
        add("demo", "info", "Fictional fixture; not a real opportunity or real eligibility decision")

    facts = call["facts"]
    trustworthy: dict[str, object] = {}
    for key in FACT_NAMES:
        fact = facts[key]
        quote = fact["evidence"]["quote"] if fact["evidence"] else None
        if fact["value"] is None:
            add(f"fact:{key}", "review", "Unknown requirement; no default has been invented")
        elif not quote or quote not in text or not integrity_ok:
            add(f"fact:{key}", "review", "Missing, changed or non-matching source excerpt")
        else:
            trustworthy[key] = fact["value"]
    for unresolved in call["unresolved_requirements"]:
        add("unresolved", "review", unresolved)

    def compare(key: str, condition: bool, yes: str, no: str) -> None:
        add(key, "pass" if condition else "fail", yes if condition else no)

    allowed = trustworthy.get("residency_allowed")
    if allowed is not None:
        if profile["residence_country"] is None:
            add("residency", "review", "Artist residence is unknown; citizenship is not a substitute")
        else:
            compare("residency", "*" in allowed or profile["residence_country"] in allowed,
                    "Supplied residence meets the normalised residency rule",
                    "Supplied residence does not meet the normalised residency rule")
    media = trustworthy.get("media_allowed")
    if media is not None:
        compare("medium", all(a["medium"] in media for a in application["artworks"]),
                "Selected media meet the normalised rule", "At least one selected medium is not accepted")
    deadline = trustworthy.get("deadline")
    if deadline:
        compare("deadline", now < utc_time(deadline), "Deadline has not passed", "Deadline has passed")
    fee = trustworthy.get("fee_amount")
    currency = trustworthy.get("fee_currency")
    if fee is not None and currency:
        if currency != profile["fee_budget"]["currency"] and fee != 0:
            add("fee", "review", "Fee and budget currencies differ; no exchange rate has been assumed")
        else:
            compare("fee", fee <= profile["fee_budget"]["amount"],
                    "Stated entry fee is within the supplied budget (not payment approval)",
                    "Stated entry fee exceeds the supplied budget")
    count = len(application["artworks"])
    lo, hi = trustworthy.get("min_images"), trustworthy.get("max_images")
    if lo is not None and hi is not None:
        if lo > hi:
            add("image_count", "fail", "Conflicting minimum/maximum image requirements")
        else:
            compare("image_count", lo <= count <= hi, "Image count meets the rule", "Image count is outside the allowed range")
    min_edge, max_edge = trustworthy.get("min_long_edge_px"), trustworthy.get("max_long_edge_px")
    if min_edge is not None and max_edge is not None and min_edge > max_edge:
        add("dimensions", "fail", "Conflicting long-edge limits")
    formats = trustworthy.get("allowed_formats")
    if formats is not None and "JPEG" not in formats:
        add("format_support", "review", "This version exports JPEG only; do not substitute JPEG for an unsupported requirement")
    filename_rule = trustworthy.get("filename_rule")
    if filename_rule is not None and filename_rule != "any":
        add("filename_support", "review", "Custom organiser filename rules need a tested adapter")
    for key, text_value in (("statement_max_words", application["statement"]),
                            ("bio_max_words", profile["approved_bio"])):
        if key in trustworthy:
            words = word_count(text_value)
            compare(key, words <= trustworthy[key], f"{words} whitespace-delimited words; within stated limit",
                    f"{words} whitespace-delimited words; exceeds stated limit")
    if not all(a["approved_for_submission_copy"] for a in application["artworks"]):
        add("artwork_permission", "review", "Each artwork needs explicit permission for submission-copy preparation")
    state = "blocked" if any(c["status"] == "fail" for c in checks) else (
        "needs_review" if any(c["status"] == "review" for c in checks) else "checks_passed")
    return {
        "status": state, "demo_only": call["demo_only"], "assessed_at": now.isoformat(),
        "opportunity_id": call["id"], "checks": checks,
        "scope": "Checks supplied structured facts and excerpt integrity; does not verify extraction meaning, source authenticity or all eligibility conditions.",
        "human_review_required": True,
        "human_review_items": [
            "Confirm complete/current organiser rules and the meaning of every extracted fact.",
            "Review rights/licensing, artist facts, statement, visual quality and any portal-specific requirements.",
            "Confirm colour conversion, metadata removal and text counts in the actual submission portal.",
            "Approve exact files, destination and any fee separately; this tool never submits or pays.",
        ],
    }
