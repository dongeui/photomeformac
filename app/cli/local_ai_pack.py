#!/usr/bin/env python3
"""Prepare and verify Photome's optional local AI pack.

This script is intentionally outside app startup. The base package can run
without PyTorch/OpenCLIP, while this command gives installers and operators a
single place to prepare model caches before offline operation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from app.services.embedding import clip as clip_embedding


def _default_cache_root() -> Path:
    explicit = os.environ.get("PHOTOME_MODEL_CACHE_ROOT")
    if explicit:
        return Path(explicit).expanduser()
    data_root = os.environ.get("PHOTOME_DATA_ROOT")
    if data_root:
        return Path(data_root).expanduser() / "models"
    return Path.cwd() / "data" / "models"


def _configure_cache(cache_root: Path, *, offline: bool) -> None:
    cache_root = cache_root.expanduser().resolve()
    hf_home = cache_root / "hf"
    torch_home = cache_root / "torch"
    xdg_cache_home = cache_root / "xdg"
    for path in (hf_home, torch_home, xdg_cache_home):
        path.mkdir(parents=True, exist_ok=True)

    os.environ["HF_HOME"] = str(hf_home)
    os.environ["TORCH_HOME"] = str(torch_home)
    os.environ["XDG_CACHE_HOME"] = str(xdg_cache_home)
    os.environ["PHOTOME_CLIP_ENABLED"] = "1"
    if offline:
        os.environ["PHOTOME_OFFLINE_MODE"] = "1"
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    else:
        os.environ["PHOTOME_OFFLINE_MODE"] = "0"
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)


def _cache_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _snapshot(cache_root: Path) -> dict[str, Any]:
    status = clip_embedding.status()
    cache_bytes = _cache_size_bytes(cache_root)
    return {
        "model": status["config"],
        "dependencies": status["dependencies"],
        "cache_root": str(cache_root),
        "cache": status["cache"],
        "cache_size_bytes": cache_bytes,
        "cache_size": _format_size(cache_bytes),
        "model_ready": status["model_ready"],
        "model_error": status["model_error"],
    }


def _deps_ready(snapshot: dict[str, Any]) -> bool:
    deps = snapshot["dependencies"]
    return all(deps.get(name) == "installed" for name in ("open_clip_torch", "torch", "torchvision"))


def _run(args: argparse.Namespace) -> int:
    cache_root = args.cache_root.expanduser().resolve()
    offline = args.command == "verify-offline"
    _configure_cache(cache_root, offline=offline)

    before = _snapshot(cache_root)
    should_load = args.command in {"prepare", "verify-offline"}
    if should_load:
        if not _deps_ready(before):
            print(json.dumps({**before, "ok": False, "error": "missing local AI pack dependencies"}, ensure_ascii=False, indent=2))
            return 2
        try:
            clip_embedding.ensure_models()
        except Exception as exc:
            after = _snapshot(cache_root)
            print(json.dumps({**after, "ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 3

    after = _snapshot(cache_root)
    ok = _deps_ready(after) and (not should_load or after["model_ready"])
    print(json.dumps({**after, "ok": ok}, ensure_ascii=False, indent=2))
    return 0 if ok or args.command == "status" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or verify Photome local AI model cache.")
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=_default_cache_root(),
        help="Model cache root. Defaults to PHOTOME_MODEL_CACHE_ROOT, PHOTOME_DATA_ROOT/models, or ./data/models.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Print dependency and cache status without loading the model.")
    sub.add_parser("prepare", help="Allow online model download/cache fill and load-test the model.")
    sub.add_parser("verify-offline", help="Force offline flags and verify the cached model can load.")
    return _run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
