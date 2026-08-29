"""Experiment storage — append-only run artifacts with content hashes."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schemas import RunManifest, TurnResult


def content_hash(data: Any) -> str:
    blob = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def prompt_dir_hash(variant_dir: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(variant_dir.rglob("*.md")):
        rel = path.relative_to(variant_dir).as_posix()
        h.update(rel.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"exp_{stamp}_{uuid.uuid4().hex[:6]}"


class ExperimentStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def create_run(self, manifest: RunManifest) -> Path:
        d = self.run_dir(manifest.run_id)
        if d.exists():
            raise FileExistsError(f"run already exists (append-only): {d}")
        d.mkdir(parents=True)
        (d / "traces").mkdir()
        (d / "results").mkdir()
        path = d / "manifest.json"
        payload = manifest.to_dict()
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        (d / "manifest.sha256").write_text(content_hash(payload) + "\n", encoding="utf-8")
        return d

    def append_turn(self, run_id: str, shift_id: str, turn: TurnResult) -> None:
        """Append a turn result. Existing files are never rewritten in place —
        we append JSONL lines only."""
        path = self.run_dir(run_id) / "results" / f"{shift_id}.jsonl"
        line = json.dumps(turn.to_dict(), default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def write_raw_trace(self, run_id: str, shift_id: str, events: Iterable[dict]) -> Path:
        path = self.run_dir(run_id) / "traces" / f"{shift_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, default=str) + "\n")
        return path

    def load_turns(self, run_id: str, shift_id: str) -> list[dict]:
        path = self.run_dir(run_id) / "results" / f"{shift_id}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def load_manifest(self, run_id: str) -> dict:
        return json.loads((self.run_dir(run_id) / "manifest.json").read_text(encoding="utf-8"))

    def list_runs(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def seal_run(self, run_id: str, totals: dict) -> None:
        """Write totals once into a separate sealed file — never mutate manifest."""
        d = self.run_dir(run_id)
        payload = {"totals": totals, "sealed_at": datetime.now(timezone.utc).isoformat()}
        path = d / "totals.json"
        if path.exists():
            raise FileExistsError("totals already sealed")
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        (d / "totals.sha256").write_text(content_hash(payload) + "\n", encoding="utf-8")
