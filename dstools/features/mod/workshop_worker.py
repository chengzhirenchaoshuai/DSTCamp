"""Steam Workshop 短生命周期工作进程。

SteamAPI_Init 会把宿主登记为 AppID 322330，GUI 长期存活会让 Steam 一直显示"正在运行饥荒"，
所以所有 UGC 查询和更新都在本进程完成，写回结果后立即退出。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from dstools.features.mod.workshop_api import (
    _download_result_to_payload,
    _get_workshop_item_snapshot_in_process,
    _snapshot_to_payload,
    _subscribe_workshop_items_in_process,
    _unsubscribe_workshop_items_in_process,
    _update_workshop_items_in_process,
)


def run_request(request: dict, emit) -> dict:
    action = request.get("action")
    dll_path = Path(request["dll_path"]) if request.get("dll_path") else None
    if action == "snapshot":
        states, installs, details = _get_workshop_item_snapshot_in_process(
            request.get("ids") or (),
            detail_ids=request.get("detail_ids") or (),
            dll_path=dll_path,
            detail_timeout=float(request.get("detail_timeout", 20.0)),
            include_subscribed=bool(request.get("include_subscribed", False)),
        )
        return _snapshot_to_payload(states, installs, details)
    if action == "update":
        batch = _update_workshop_items_in_process(
            request.get("ids") or (),
            dll_path=dll_path,
            timeout=float(request.get("timeout", 180.0)),
            expected_versions={
                int(key): str(value)
                for key, value in (request.get("expected_versions") or {}).items()
            },
            force_redownload_ids={
                int(item) for item in (request.get("force_redownload_ids") or ())
            },
            on_progress=lambda current, total, done, size: emit(
                {
                    "type": "progress",
                    "current": current,
                    "total": total,
                    "downloaded": done,
                    "size": size,
                }
            ),
            on_item_start=lambda current, total, workshop_id: emit(
                {
                    "type": "item_start",
                    "current": current,
                    "total": total,
                    "workshop_id": workshop_id,
                }
            ),
            on_item_complete=lambda current, total, result: emit(
                {
                    "type": "item_complete",
                    "current": current,
                    "total": total,
                    "result": _download_result_to_payload(result),
                }
            ),
        )
        return {
            "results": [_download_result_to_payload(item) for item in batch.results]
        }
    if action == "subscribe":
        results = _subscribe_workshop_items_in_process(
            request.get("ids") or (), dll_path=dll_path
        )
        return {"results": {str(key): value for key, value in results.items()}}
    if action == "unsubscribe":
        results = _unsubscribe_workshop_items_in_process(
            request.get("ids") or (), dll_path=dll_path
        )
        return {"results": {str(key): value for key, value in results.items()}}
    raise ValueError(f"未知 Steam Worker 操作：{action}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        return 2
    request_path, event_path, result_path = map(Path, args)

    def emit(event: dict) -> None:
        with event_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        response = {"ok": True, "result": run_request(request, emit)}
    except Exception as exc:
        response = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    temp_result = result_path.with_suffix(".tmp")
    temp_result.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    temp_result.replace(result_path)
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
