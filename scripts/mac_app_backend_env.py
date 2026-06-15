#!/usr/bin/env python3
"""Mac 앱 런타임용 Trove 백엔드 환경 변수 생성기."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable


def _abs(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve())


def build_backend_env(
    app_data_root: Path | str,
    *,
    source_roots: Iterable[Path | str] | None = None,
    port: int = 8000,
    clip_enabled: bool = True,
    offline_mode: bool = True,
) -> dict[str, str]:
    """Mac 앱에서 로컬 Trove 백엔드를 실행할 때 쓸 환경 변수를 만든다.

    Docker 경로(`/photos`)로 source root를 바꾸지 않고, 사용자가 고른 macOS
    경로를 그대로 넘긴다. Mac 앱은 항상 local-only(127.0.0.1)로 바인딩한다 —
    LAN 공유는 제거됐다. 네트워크 노출이 필요하면 Docker/서버 배포를 쓴다.
    """
    root = Path(app_data_root).expanduser().resolve()
    data_root = root / "data"
    derived_root = root / "derived"
    model_root = root / "models"

    env = {
        "TROVE_SERVER_HOST": "127.0.0.1",
        "TROVE_SERVER_PORT": str(port),
        "TROVE_DATA_ROOT": str(data_root),
        "TROVE_DERIVED_ROOT": str(derived_root),
        "TROVE_MODEL_ROOT": str(model_root),
        "TROVE_GEODATA_ROOT": str(model_root / "geodata"),
        "TROVE_DATABASE_PATH": str(data_root / "photome.sqlite3"),
        "TROVE_OFFLINE_MODE": "1" if offline_mode else "0",
        "TROVE_CLIP_ENABLED": "1" if clip_enabled else "0",
        # 통합 동기화(스캔+이미지 AI) 스케줄러 — CLIP 없는 빌드에서도 스캔은
        # 자동으로 돌아야 하므로 항상 켠다.
        "TROVE_SYNC_SCHEDULER_ENABLED": "1",
        "TROVE_ENV_FILE": str(root / "photome.env"),
        # 맥앱이 supervisor임을 알린다 → 백엔드가 부모 사망을 감지해 자가 종료
        # (고아 백엔드 누적 방지, app/core/single_instance.py).
        "TROVE_SUPERVISED": "1",
        "HF_HOME": str(model_root / "huggingface"),
        "TORCH_HOME": str(model_root / "torch"),
    }

    if source_roots:
        env["TROVE_SOURCE_ROOTS"] = ",".join(_abs(path) for path in source_roots)

    return env


def main() -> None:
    parser = argparse.ArgumentParser(description="Trove Mac 앱 백엔드 env 생성")
    parser.add_argument("app_data_root", help="앱 데이터 루트")
    parser.add_argument("--source-root", action="append", default=[], help="원본 사진 폴더")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-clip", action="store_true", help="CLIP 비활성화")
    parser.add_argument("--online", action="store_true", help="오프라인 모드 해제")
    args = parser.parse_args()

    env = build_backend_env(
        args.app_data_root,
        source_roots=args.source_root,
        port=args.port,
        clip_enabled=not args.no_clip,
        offline_mode=not args.online,
    )
    print(json.dumps(env, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
