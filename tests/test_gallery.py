"""Regression tests for the persistent result gallery.

Run with:
    RHW_DATA_DIR=/tmp/rhw-gallery-test RHW_SKIP_BOOTSTRAP=1 \
        python tests/test_gallery.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="rhw-gallery-"))
os.environ["RHW_DATA_DIR"] = str(TMP / "data")
os.environ["RHW_SKIP_BOOTSTRAP"] = "1"

import app  # noqa: E402

DOWNLOADS = TMP / "downloads"
DOWNLOADS.mkdir(parents=True, exist_ok=True)
app.SETTINGS.update({"download_dir": str(DOWNLOADS)})

WORKFLOW = {"12": {"class_type": "KSampler", "inputs": {"seed": 12345, "steps": 20}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat"}}}


def make_job(status, results, auto_download=1):
    return app.DB.enqueue({
        "host": "ai", "workflowId": "999", "profileName": "测试工作流",
        "overrides": [{"nodeId": "6", "fieldName": "text", "fieldValue": "a cat"}],
        "workflow": WORKFLOW, "runs": 1, "autoDownload": bool(auto_download),
    })[0]


def set_status(job_id, status, results=None):
    app.DB.update_job(job_id, status=status, results=results or [])


def test_archive_and_survive_cleanup():
    job = make_job("QUEUED", [])
    set_status(job["id"], "SUCCESS", results=[
        {"fileUrl": "https://x.test/a.png", "fileType": "png", "nodeId": "9"},
    ])
    assert app.DB.archive_job_results(job["id"]) == 1

    pending = make_job("QUEUED", [])
    running = make_job("QUEUED", [])
    set_status(running["id"], "RUNNING")

    removed = app.DB.clear_finished()
    assert removed == 1, f"cleanup removed {removed} jobs, expected only the finished one"
    assert app.DB.get_job(pending["id"]) is not None, "QUEUED job was deleted"
    assert app.DB.get_job(running["id"]) is not None, "RUNNING job was deleted"
    assert app.DB.get_job(job["id"]) is None, "finished job should be gone"

    items = [item for item in app.DB.list_gallery() if item["jobId"] == job["id"]]
    assert len(items) == 1, "gallery lost the result after cleanup"
    assert items[0]["profileName"] == "测试工作流"
    assert items[0]["fileType"] == "png"
    print("  ✓ 归档结果在清理队列后依然保留")


def test_local_file_archived():
    target = DOWNLOADS / "2026-09-04" / "abcdef123456_1.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    job = make_job("QUEUED", [])
    set_status(job["id"], "SUCCESS", results=[
        {"fileUrl": "https://x.test/a.png", "fileType": "png",
         "localPath": str(target), "localUrl": f"/api/jobs/{job['id']}/result/0"},
    ])
    app.DB.archive_job_results(job["id"])
    app.DB.clear_finished()

    item = next(i for i in app.DB.list_gallery() if i["jobId"] == job["id"])
    assert item["hasLocal"] is True
    assert item["localUrl"] == f"/api/gallery/{item['id']}/file"
    assert app.DB.gallery_file_target(item["id"]) == target.resolve()

    restore = app.DB.gallery_restore_payload(item["id"])
    assert restore["workflow"]["6"]["inputs"]["text"] == "a cat"
    info = app.DB.gallery_generation_info(item["id"])
    assert info["parameters"] or info["other"] or info["prompts"]
    print("  ✓ 本地文件、工作流恢复与生成信息均可独立读取")


def test_import_local_files_is_idempotent():
    before = len(app.DB.list_gallery())
    first = app.DB.import_local_files(app.SETTINGS.load())
    second = app.DB.import_local_files(app.SETTINGS.load())
    after = len(app.DB.list_gallery())
    ids = {item["id"] for item in app.DB.list_gallery()}
    assert len(ids) == after, "导入产生了重复条目"
    assert after == before, f"重复扫描新增了条目：{before} -> {after}"
    print(f"  ✓ 本地文件导入幂等（扫描 {first}/{second} 个文件，条目数不变：{after}）")


def test_clear_keeps_cancelling_job():
    job = make_job("QUEUED", [])
    set_status(job["id"], "RUNNING")
    app.DB.request_cancel(job["id"])
    assert app.DB.clear_finished() == 0
    assert app.DB.get_job(job["id"]) is not None
    print("  ✓ 取消中的任务不会被清理")


def test_clear_skips_in_flight_job():
    job = make_job("QUEUED", [])
    set_status(job["id"], "SUCCESS", results=[{"fileUrl": "https://x.test/b.png", "fileType": "png"}])
    with app.ACTIVE_JOB_LOCK:
        app.ACTIVE_JOB_IDS.add(job["id"])
    try:
        assert app.DB.clear_finished() == 0
        assert app.DB.get_job(job["id"]) is not None
    finally:
        with app.ACTIVE_JOB_LOCK:
            app.ACTIVE_JOB_IDS.discard(job["id"])
    print("  ✓ 正在处理中的任务不会被清理")


def main():
    for case in [
        test_archive_and_survive_cleanup,
        test_local_file_archived,
        test_import_local_files_is_idempotent,
        test_clear_keeps_cancelling_job,
        test_clear_skips_in_flight_job,
    ]:
        case()
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
