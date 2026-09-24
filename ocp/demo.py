"""Fictional first-run sample. Geometric test images, not generated artwork."""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

from .io import ProducerError, load_json, sha256, write_json


def create_demo(out: Path, scenario: str = "ready") -> dict:
    target = out.absolute()
    for part in (target, *target.parents):
        if part.is_symlink():
            raise ProducerError("Do not create demos inside symlinked paths")
    if target.exists():
        raise ProducerError("Demo destination already exists; choose a new empty path")
    example_root = Path(__file__).resolve().parents[1] / "examples"
    source = (example_root / "fictional-call.md").read_text(encoding="utf-8")
    call = copy.deepcopy(load_json(example_root / "opportunity.example.json"))
    if scenario == "ineligible":
        source = source.replace("Eligible residents: SE and FI.", "Eligible residents: US only.")
        call["facts"]["residency_allowed"] = {"value": ["US"], "evidence": {"quote": "Eligible residents: US only.", "locator": "Eligibility"}}
    elif scenario == "unknown":
        call["facts"]["deadline"] = {"value": None, "evidence": None}
        call["unresolved_requirements"] = ["The example intentionally withholds the deadline; contact the fictional organiser in the scenario, not a real person."]
    elif scenario == "expired":
        source = source.replace("2099-06-30T23:59:00+02:00", "2020-06-30T23:59:00+02:00")
        call["facts"]["deadline"] = {"value": "2020-06-30T23:59:00+02:00", "evidence": {"quote": "Deadline: 2020-06-30T23:59:00+02:00.", "locator": "Deadline"}}
    elif scenario != "ready":
        raise ProducerError("Unknown demo scenario")
    target.mkdir(parents=True)
    try:
        (target / "sources").mkdir()
        (target / "originals").mkdir()
        snapshot = target / "sources/call.md"
        snapshot.write_text(source, encoding="utf-8")
        call["source"]["snapshot_sha256"] = sha256(snapshot)
        write_json(target / "opportunity.json", call)
        for name in ("profile", "application"):
            write_json(target / f"{name}.json", load_json(example_root / f"{name}.example.json"))
        sizes = [(2200, 1500), (1500, 2200), (2100, 1400)]
        for number, size in enumerate(sizes, 1):
            image = Image.new("RGB", size, (220, 220, 220))
            draw = ImageDraw.Draw(image)
            step = 80 + number * 10
            for x in range(0, size[0], step):
                shade = (x // step * 31 + number * 40) % 220
                draw.rectangle((x, 0, min(x + step // 2, size[0] - 1), size[1] - 1), fill=(shade, shade, shade))
            draw.rectangle((size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4), outline=(255, 255, 255), width=18)
            image.save(target / f"originals/demo-{number:02d}.png")
        return {"workspace": str(out), "scenario": scenario, "demo_only": True,
                "next": "Run assess, then prepare only when checks pass. No real opportunity is represented."}
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
