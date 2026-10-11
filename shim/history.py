"""Immutable completed compilations, plus an atomic latest-manifest pointer."""
import asyncio
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .manifest import Manifest

_locks = {}


def _root(host):
    from .compile import SITES_DIR
    if not re.fullmatch(r"[A-Za-z0-9._:\[\]-]+", host) or host in {".", ".."}:
        raise ValueError("Invalid site host")
    return SITES_DIR / host


def new_run(host):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:12]
    directory = _root(host) / "history" / run_id
    (directory / "raw").mkdir(parents=True)
    return run_id, directory


def version_dir(host, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid compilation version")
    return _root(host) / "history" / run_id


def load_version(host, run_id):
    return Manifest.model_validate_json((version_dir(host, run_id) / "manifest.json").read_text())


def list_versions(host):
    versions = []
    for path in sorted((_root(host) / "history").glob("*/manifest.json"), reverse=True):
        try:
            manifest = Manifest.model_validate_json(path.read_text())
            versions.append({"run_id": path.parent.name, "compiled_at": manifest.compiled_at,
                             "actions": len(manifest.actions),
                             "formats": sorted(p.name for p in (path.parent / "agent").iterdir() if p.is_file())})
        except (OSError, ValueError):
            continue
    return versions


async def publish_run(manifest, run_dir):
    from .emit import emit_all
    root = _root(manifest.host)
    if run_dir != version_dir(manifest.host, manifest.run_id):
        raise ValueError("Run directory does not match manifest version")
    if (run_dir / "manifest.json").exists():
        raise ValueError("Compilation version is immutable")
    emit_all(manifest, run_dir / "agent")
    # A manifest is the completion marker. Failed emissions never appear as saved runs.
    temporary = run_dir / "manifest.pending"
    temporary.write_text(manifest.to_json())
    os.replace(temporary, run_dir / "manifest.json")
    lock = _locks.setdefault(str(root.resolve()), asyncio.Lock())
    async with lock:
        latest = root / "manifest.json"
        if latest.exists():
            old = Manifest.model_validate_json(latest.read_text())
            if not old.run_id:
                legacy_id, archive = new_run(manifest.host)
                old.run_id = legacy_id
                if (root / "agent").exists():
                    shutil.copytree(root / "agent", archive / "agent")
                else:
                    emit_all(old, archive / "agent")
                (archive / "manifest.json").write_text(old.to_json())
        # Version-aware readers never depend on this compatibility copy.
        shutil.copytree(run_dir / "agent", root / "agent", dirs_exist_ok=True)
        temporary = root / (manifest.run_id + ".pending")
        temporary.write_text(manifest.to_json())
        os.replace(temporary, latest)
    return root / "manifest.json"
