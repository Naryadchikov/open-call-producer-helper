"""Draft packaging and independent disk/ZIP revalidation. Never sends anything."""
from __future__ import annotations

import hashlib
import html
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from .assessment import assess, load_workspace
from .imaging import create_jpeg, inspect_jpeg
from .io import ProducerError, load_json, local_path, sha256, slug, write_json

TEXT_FILES = ("artist-statement.txt", "artist-bio.txt", "image-list.txt")


def input_hashes(root: Path, call: dict) -> dict[str, str]:
    names = ["profile.json", "opportunity.json", "application.json", call["source"]["snapshot_path"]]
    return {name: sha256(local_path(root, name)) for name in names}


def image_names(application: dict) -> list[str]:
    return [f"images/{i:02d}-{art['id']}.jpg" for i, art in enumerate(application["artworks"], 1)]


def image_list(application: dict) -> str:
    parts = []
    for i, art in enumerate(application["artworks"], 1):
        parts.append(f"{i:02d}. {art['title']} ({art['year']})\n"
                     f"ID: {art['id']}\nMedium: {art['medium']}\nCredit: {art['credit']}\n")
    return "\n".join(parts)


def make_zip(submission: Path, target: Path, names: list[str]) -> None:
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, local_path(submission, name).read_bytes())


def prepare(workspace: Path, name: str) -> dict:
    root, profile, call, application = load_workspace(workspace)
    name = slug(name)
    assessment = assess(root)
    if assessment["status"] != "checks_passed":
        raise ProducerError(f"Preparation stopped: {assessment['status']}. Run assess and resolve its findings; do not invent evidence.")
    # Caller supplies only a run name, never an arbitrary output path.
    output_root = local_path(root, "output", must_exist=False)
    output_root.mkdir(exist_ok=True)
    target = local_path(root, f"output/{name}", must_exist=False)
    if target.exists():
        raise ProducerError("Output already exists. Choose a new run name; nothing is overwritten.")
    temporary = Path(tempfile.mkdtemp(prefix=".preparing-", dir=output_root))
    try:
        submission = temporary / "submission"
        (submission / "images").mkdir(parents=True)
        review = temporary / "review"
        review.mkdir()
        image_paths = image_names(application)
        rows = []
        for art, image_path in zip(application["artworks"], image_paths):
            original = local_path(root, art["path"])
            original_hash = sha256(original)
            result = create_jpeg(original, submission / image_path, call["facts"], application["untagged_colour_policy"])
            if sha256(original) != original_hash or result["source_sha256"] != original_hash:
                raise ProducerError("Source changed while preparing a derivative; discard this run")
            rows.append({"artwork_id": art["id"], "path": image_path, **result})
        (submission / "artist-statement.txt").write_text(application["statement"] + "\n", encoding="utf-8")
        (submission / "artist-bio.txt").write_text(profile["approved_bio"] + "\n", encoding="utf-8")
        (submission / "image-list.txt").write_text(image_list(application), encoding="utf-8")
        names = image_paths + list(TEXT_FILES)
        manifest = {
            "schema_version": 1, "state": "DRAFT_AWAITING_HUMAN_REVIEW", "demo_only": call["demo_only"],
            "input_hashes": input_hashes(root, call), "images": rows,
            "files": [{"path": path, "bytes": (submission / path).stat().st_size,
                       "sha256": sha256(submission / path)} for path in sorted(names)],
            "integrity_note": "Hashes bind this local draft to its inputs; not a digital signature or independent eligibility proof.",
        }
        write_json(temporary / "manifest.json", manifest)
        write_json(review / "assessment.json", assessment)
        write_json(review / "requirements.json", {"source": call["source"], "facts": call["facts"],
                                                   "unresolved_requirements": call["unresolved_requirements"]})
        report_lines = ["# Submission preparation report", "", "**DRAFT — NOT SUBMITTED. Human review required.**", "",
                        f"Opportunity: {call['title']}", f"Demo only: {call['demo_only']}", "", assessment["scope"], ""]
        report_lines += [f"- {c['status'].upper()}: {c['id']} — {c['message']}" for c in assessment["checks"]]
        report_lines += ["", "## Required human checks", ""] + [f"- [ ] {item}" for item in assessment["human_review_items"]]
        report_lines += ["", "## Evidence", "", "Literal excerpts are checked against the snapshot, not semantically verified.", ""]
        for key, fact in call["facts"].items():
            report_lines.extend([f"### {key}", f"Value: `{json.dumps(fact['value'], ensure_ascii=False)}`",
                                 f"Locator: {fact['evidence']['locator']}", f"> {fact['evidence']['quote']}", ""])
        (review / "report.md").write_text("\n".join(report_lines), encoding="utf-8")
        cards = []
        for art, row in zip(application["artworks"], rows):
            cards.append(f'<figure><img src="../submission/{row["path"]}" alt="{html.escape(art["title"], quote=True)}">'
                         f'<figcaption>{html.escape(art["title"])} · {row["width"]} × {row["height"]} px · '
                         f'{row["bytes"]:,} bytes</figcaption></figure>')
        page = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>Submission proof — human review</title><style>body{font:16px system-ui;margin:32px;max-width:1100px}'
                'main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px}'
                'figure{margin:0}img{width:100%;height:280px;object-fit:contain}figcaption{padding:12px 0}</style>'
                '<h1>Submission proof</h1><p>DRAFT — NOT SUBMITTED. Inspect every image before approval.</p>'
                f'<p>{html.escape(call["title"])}</p><main>{"".join(cards)}</main></html>')
        (review / "contact-sheet.html").write_text(page, encoding="utf-8")
        make_zip(submission, temporary / "submission.zip", names)
        # Validate the temporary draft too, before it becomes the named deliverable.
        validate(root, temporary.relative_to(root).as_posix())
        if target.exists():
            raise ProducerError("Output appeared during preparation; choose a new run name")
        temporary.rename(target)
        return {"state": manifest["state"], "package": f"output/{name}", "demo_only": call["demo_only"],
                "images": len(rows), "submitted": False}
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def validate(workspace: Path, package: str) -> dict:
    root, profile, call, application = load_workspace(workspace)
    folder = local_path(root, package, must_exist=False)
    if not folder.is_dir():
        raise ProducerError("Package directory not found")
    for child in folder.rglob("*"):
        if child.is_symlink():
            raise ProducerError("Symlink found in package")
    assessment = assess(root)
    if assessment["status"] != "checks_passed":
        raise ProducerError(f"Current opportunity/input check is {assessment['status']}; draft is not current")
    manifest = load_json(local_path(folder, "manifest.json"))
    if manifest.get("schema_version") != 1 or manifest.get("state") != "DRAFT_AWAITING_HUMAN_REVIEW":
        raise ProducerError("Unrecognised draft manifest")
    if manifest.get("demo_only") != call["demo_only"] or manifest.get("input_hashes") != input_hashes(root, call):
        raise ProducerError("Inputs changed since preparation; create a new draft")
    submission = local_path(folder, "submission", must_exist=False)
    expected = set(image_names(application)) | set(TEXT_FILES)
    actual = {p.relative_to(submission).as_posix() for p in submission.rglob("*") if p.is_file()}
    if actual != expected:
        raise ProducerError("Unexpected or missing submission files")
    rows = manifest.get("files", [])
    if len(rows) != len(expected) or {row["path"] for row in rows} != expected:
        raise ProducerError("Manifest file list does not match required files")
    for row in rows:
        path = local_path(submission, row["path"])
        if path.stat().st_size != row["bytes"] or sha256(path) != row["sha256"]:
            raise ProducerError(f"Changed file: {row['path']}")
    images = manifest.get("images", [])
    if len(images) != len(application["artworks"]):
        raise ProducerError("Manifest image count mismatch")
    for art, name, row in zip(application["artworks"], image_names(application), images):
        if row["artwork_id"] != art["id"] or row["path"] != name:
            raise ProducerError("Manifest image identity/order mismatch")
        if row["source_sha256"] != sha256(local_path(root, art["path"])):
            raise ProducerError("Original image changed since preparation")
        measured = inspect_jpeg(local_path(submission, name), call["facts"])
        if any(row.get(key) != measured[key] for key in measured):
            raise ProducerError("Manifest image measurements no longer match disk")
    expected_text = {"artist-statement.txt": application["statement"] + "\n",
                     "artist-bio.txt": profile["approved_bio"] + "\n", "image-list.txt": image_list(application)}
    for name, content in expected_text.items():
        if local_path(submission, name).read_text(encoding="utf-8") != content:
            raise ProducerError("Submission text differs from the current approved inputs/draft statement")
    archive_path = local_path(folder, "submission.zip")
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ProducerError("ZIP has unexpected, missing or duplicate entries")
        for row in rows:
            info = archive.getinfo(row["path"])
            if info.file_size != row["bytes"]:
                raise ProducerError("ZIP entry size mismatch")
            with archive.open(info) as stream:
                data = stream.read(row["bytes"] + 1)
            if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ProducerError("ZIP entry differs from measured submission file")
    return {"status": "validated_draft", "images": len(images), "demo_only": call["demo_only"],
            "human_review_required": True, "submitted": False,
            "note": "Checks actual files and ZIP against normalised inputs. Not legal clearance or proof of complete eligibility."}
