#!/usr/bin/env python3
"""RunningHub Desk — local workflow queue and result manager.

This application intentionally uses only Python's standard library. It binds to
127.0.0.1, stores task history in SQLite, and keeps API keys in macOS Keychain
when available (with a permission-restricted file fallback on other systems).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import platform
import random
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
import webbrowser
import zlib
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
DEFAULT_DATA_ROOT = APP_ROOT / "data"
DATA_ROOT = Path(os.environ.get("RHW_DATA_DIR", DEFAULT_DATA_ROOT)).expanduser().resolve()
DB_PATH = DATA_ROOT / "runninghub.db"
SETTINGS_PATH = DATA_ROOT / "settings.json"
FALLBACK_KEYS_PATH = DATA_ROOT / ".keys.json"

HOSTS = {
    "ai": "www.runninghub.ai",
    "cn": "www.runninghub.cn",
}
KEYCHAIN_SERVICE = "cn.codex.runninghub-desk"
# The legacy workflow output endpoint returns 804 while the same API key's task
# is still executing. It is a polling state, despite the error-like code/name.
TRANSIENT_OUTPUT_CODES = {"804"}
JS_MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_TEXT_PREVIEW_BYTES = 4 * 1024 * 1024

# Jobs in these states are counted as "finished" and may be removed from the
# queue. Anything else (queued / submitting / running / downloading) must never
# be touched by the cleanup action.
FINISHED_JOB_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")
ACTIVE_JOB_STATUSES = ("QUEUED", "SUBMITTING", "RUNNING", "DOWNLOADING")
# Job ids a worker thread is currently processing. Cleanup skips them even if
# their stored status somehow looks finished.
ACTIVE_JOB_LOCK = threading.Lock()
ACTIVE_JOB_IDS: set[str] = set()


def decode_text_preview(data: bytes) -> str:
    if len(data) > MAX_TEXT_PREVIEW_BYTES:
        raise ValueError("文本结果超过 4MB，无法在页面内预览")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig", errors="replace")

DEFAULT_SETTINGS = {
    "default_host": "ai",
    "default_workflow_id": "",
    "poll_interval": 5,
    "timeout_minutes": 60,
    "download_dir": str(APP_ROOT / "downloads"),
    "queue_paused": False,
    "auto_download": True,
    "send_full_workflow": False,
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def safe_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def sanitize_filename(value: str, fallback: str = "result") -> str:
    value = Path(value).name
    value = re.sub(r"[^\w.()\- ]+", "_", value, flags=re.UNICODE).strip(" .")
    return value[:180] or fallback


def media_preview_path(host: str, remote_name: str) -> Path:
    suffix = Path(urlparse(remote_name).path).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp"}:
        suffix = ".bin"
    digest = hashlib.sha256(f"{host}:{remote_name}".encode("utf-8")).hexdigest()
    return DATA_ROOT / "media-previews" / f"{digest}{suffix}"


def read_png_json_text(path: Path, key: str) -> dict[str, Any] | None:
    """Read a JSON tEXt/zTXt/iTXt value from a PNG without third-party packages."""
    try:
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            while True:
                header = handle.read(8)
                if len(header) != 8:
                    return None
                length, chunk_type = struct.unpack(">I4s", header)
                if length > 64 * 1024 * 1024:
                    return None
                data = handle.read(length)
                handle.read(4)
                text_key: bytes | None = None
                text_value: bytes | None = None
                if chunk_type == b"tEXt" and b"\0" in data:
                    text_key, text_value = data.split(b"\0", 1)
                elif chunk_type == b"zTXt" and b"\0" in data:
                    text_key, compressed = data.split(b"\0", 1)
                    if len(compressed) > 1:
                        text_value = zlib.decompress(compressed[1:])
                elif chunk_type == b"iTXt" and b"\0" in data:
                    parts = data.split(b"\0", 5)
                    if len(parts) == 6:
                        text_key = parts[0]
                        text_value = zlib.decompress(parts[5]) if parts[1] == b"\x01" else parts[5]
                if text_key and text_key.decode("latin-1") == key and text_value is not None:
                    value = json.loads(text_value.decode("utf-8"))
                    return value if isinstance(value, dict) else None
                if chunk_type == b"IEND":
                    return None
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, zlib.error):
        return None


def browser_safe_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """Keep large seed values exact when JSON is consumed by JavaScript."""
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for field_name, value in list(inputs.items()):
            if "seed" in field_name.lower() and isinstance(value, int) and abs(value) > JS_MAX_SAFE_INTEGER:
                inputs[field_name] = str(value)
    return workflow


def normalize_workflow_seeds(workflow: dict[str, Any]) -> dict[str, Any]:
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for field_name, value in list(inputs.items()):
            if "seed" in field_name.lower() and isinstance(value, str):
                try:
                    inputs[field_name] = int(value)
                except ValueError:
                    pass
    return workflow


def summarize_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    prompts: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    loras: list[dict[str, Any]] = []
    parameters: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    parameter_names = {
        "seed", "noise_seed", "steps", "cfg", "denoise", "sampler_name", "scheduler",
        "width", "height", "aspect_ratio", "megapixels", "multiple", "batch_size",
        "duration", "frame_rate", "crf", "format",
    }
    model_names = {
        "ckpt_name", "checkpoint", "checkpoint_name", "unet_name", "model_name",
        "clip_name", "vae_name", "control_net_name",
    }
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "Unknown")
        title = str((node.get("_meta") or {}).get("title") or class_type)
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        lowered = f"{class_type} {title}".lower()
        scalar_inputs = {
            str(name): value for name, value in inputs.items()
            if value is None or isinstance(value, (str, int, float, bool))
        }
        if "lora" in lowered or "lora_name" in scalar_inputs:
            loras.append({
                "nodeId": str(node_id), "title": title,
                "name": scalar_inputs.get("lora_name") or scalar_inputs.get("name") or "LoRA",
                "strengthModel": scalar_inputs.get("strength_model"),
                "strengthClip": scalar_inputs.get("strength_clip"),
            })
        for field_name, value in scalar_inputs.items():
            semantic_name = field_name
            if field_name == "value" and class_type.lower().startswith("primitive"):
                match = re.search(r"\(([^()]+)\)\s*$", title)
                if match:
                    semantic_name = re.sub(r"[\s-]+", "_", match.group(1).strip().lower())
            item = {
                "nodeId": str(node_id), "nodeTitle": title, "classType": class_type,
                "fieldName": field_name, "name": semantic_name, "value": value,
            }
            is_prompt = (
                semantic_name in {"prompt", "text", "negative_prompt"}
                and any(token in lowered for token in ("prompt", "textencode", "conditioning"))
            )
            if is_prompt and isinstance(value, str):
                item["kind"] = "negative" if "negative" in semantic_name or "negative" in lowered else "positive"
                prompts.append(item)
            elif semantic_name in model_names and "lora" not in lowered:
                models.append(item)
            elif semantic_name in parameter_names:
                parameters.append(item)
            elif not ("lora" in lowered and semantic_name in {"lora_name", "strength_model", "strength_clip"}):
                other.append(item)
    return {
        "prompts": prompts, "models": models, "loras": loras,
        "parameters": parameters, "other": other,
    }


class ApiError(RuntimeError):
    def __init__(self, message: str, *, code: Any = None, payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class SettingsStore:
    def __init__(self) -> None:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not SETTINGS_PATH.exists():
            self.save(DEFAULT_SETTINGS.copy())

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                current = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
            merged = DEFAULT_SETTINGS.copy()
            merged.update(current)
            return merged

    def save(self, value: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = DEFAULT_SETTINGS.copy()
            merged.update(value)
            merged["poll_interval"] = safe_int(merged.get("poll_interval"), 5, 2, 60)
            merged["timeout_minutes"] = safe_int(merged.get("timeout_minutes"), 60, 5, 1440)
            path = Path(str(merged.get("download_dir") or DEFAULT_SETTINGS["download_dir"]))
            merged["download_dir"] = str(path.expanduser().resolve())
            SETTINGS_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            return merged

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        value = self.load()
        allowed = set(DEFAULT_SETTINGS)
        value.update({k: v for k, v in patch.items() if k in allowed})
        return self.save(value)


class KeyStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._use_keychain = (
            platform.system() == "Darwin"
            and shutil.which("security") is not None
            and os.environ.get("RHW_FORCE_FILE_KEYS") != "1"
        )

    @property
    def backend_name(self) -> str:
        return "macOS 钥匙串" if self._use_keychain else "本地权限文件"

    def get(self, host_key: str) -> str | None:
        if host_key not in HOSTS:
            return None
        with self._lock:
            if self._use_keychain:
                proc = subprocess.run(
                    ["security", "find-generic-password", "-a", host_key, "-s", KEYCHAIN_SERVICE, "-w"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return proc.stdout.strip() if proc.returncode == 0 else None
            return self._load_file().get(host_key)

    def set(self, host_key: str, api_key: str) -> None:
        if host_key not in HOSTS:
            raise ValueError("未知站点")
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("API Key 不能为空")
        with self._lock:
            if self._use_keychain:
                proc = subprocess.run(
                    [
                        "security", "add-generic-password", "-U", "-a", host_key,
                        "-s", KEYCHAIN_SERVICE, "-w", api_key,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode != 0:
                    raise RuntimeError(proc.stderr.strip() or "写入钥匙串失败")
                return
            values = self._load_file()
            values[host_key] = api_key
            self._write_file(values)

    def delete(self, host_key: str) -> None:
        if host_key not in HOSTS:
            return
        with self._lock:
            if self._use_keychain:
                subprocess.run(
                    ["security", "delete-generic-password", "-a", host_key, "-s", KEYCHAIN_SERVICE],
                    capture_output=True,
                    check=False,
                )
                return
            values = self._load_file()
            values.pop(host_key, None)
            self._write_file(values)

    def _load_file(self) -> dict[str, str]:
        try:
            return json.loads(FALLBACK_KEYS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_file(self, values: dict[str, str]) -> None:
        FALLBACK_KEYS_PATH.write_text(json.dumps(values), encoding="utf-8")
        os.chmod(FALLBACK_KEYS_PATH, 0o600)


class Database:
    def __init__(self) -> None:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    workflow_json TEXT NOT NULL,
                    group_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_groups (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    host TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    profile_id TEXT,
                    profile_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_index INTEGER NOT NULL,
                    run_total INTEGER NOT NULL,
                    overrides_json TEXT NOT NULL,
                    workflow_json TEXT,
                    remote_task_id TEXT,
                    results_json TEXT,
                    error TEXT,
                    message TEXT,
                    auto_download INTEGER NOT NULL DEFAULT 1,
                    send_full_workflow INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS gallery_items (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    result_index INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    host TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    profile_id TEXT,
                    profile_name TEXT NOT NULL,
                    remote_task_id TEXT,
                    node_id TEXT,
                    file_type TEXT,
                    file_name TEXT,
                    local_path TEXT,
                    remote_url TEXT,
                    result_json TEXT NOT NULL,
                    overrides_json TEXT NOT NULL,
                    workflow_ref TEXT,
                    source TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS gallery_job_result
                    ON gallery_items(job_id, result_index);
                CREATE INDEX IF NOT EXISTS gallery_created ON gallery_items(created_at);
                CREATE TABLE IF NOT EXISTS gallery_workflows (
                    ref TEXT PRIMARY KEY,
                    workflow_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            # One gallery entry per physical file. Rows without a local file are
            # remote-only results; SQLite treats their NULL path as distinct.
            conn.execute(
                """DELETE FROM gallery_items
                   WHERE local_path IS NOT NULL AND local_path <> ''
                     AND id NOT IN (
                       SELECT MIN(id) FROM gallery_items
                       WHERE local_path IS NOT NULL AND local_path <> ''
                       GROUP BY local_path
                     )"""
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS gallery_local_path ON gallery_items(local_path)"
            )
            gallery_columns = {row["name"] for row in conn.execute("PRAGMA table_info(gallery_items)")}
            if "search_text" not in gallery_columns:
                conn.execute("ALTER TABLE gallery_items ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
            profile_columns = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)")}
            if "group_id" not in profile_columns:
                conn.execute("ALTER TABLE profiles ADD COLUMN group_id TEXT")
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "send_full_workflow" not in job_columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN send_full_workflow INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "UPDATE jobs SET status='QUEUED', message='程序重启后重新排队' "
                "WHERE status IN ('SUBMITTING','DOWNLOADING')"
            )
            conn.execute(
                """UPDATE jobs
                SET status='RUNNING', error=NULL, message='恢复查询远程结果', updated_at=?
                WHERE status='FAILED' AND error='APIKEY_TASK_IS_RUNNING'
                  AND remote_task_id IS NOT NULL""",
                (now_iso(),),
            )
        self.backfill_gallery_search_text()

    def backfill_gallery_search_text(self) -> int:
        """Compute search_text for legacy gallery rows that predate the column."""
        updated = 0
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT g.id, g.profile_name, g.file_name, g.job_id, g.remote_task_id,
                          g.node_id, g.overrides_json, g.workflow_ref,
                          j.workflow_json AS job_workflow, gw.workflow_json AS ref_workflow
                   FROM gallery_items g
                   LEFT JOIN jobs j ON j.id = g.job_id
                   LEFT JOIN gallery_workflows gw ON gw.ref = g.workflow_ref
                   WHERE g.search_text = ''"""
            ).fetchall()
            for row in rows:
                workflow = self._load_json(row["job_workflow"] or row["ref_workflow"] or "")
                if not isinstance(workflow, dict):
                    workflow = None
                text = self._gallery_search_text(
                    row["profile_name"], row["file_name"], row["job_id"],
                    row["remote_task_id"], row["node_id"],
                    row["overrides_json"], workflow,
                )
                conn.execute(
                    "UPDATE gallery_items SET search_text=? WHERE id=?", (text, row["id"])
                )
                updated += 1
        return updated

    @staticmethod
    def _profile(row: sqlite3.Row) -> dict[str, Any]:
        workflow = browser_safe_workflow(json.loads(row["workflow_json"]))
        return {
            "id": row["id"], "name": row["name"], "host": row["host"],
            "workflowId": row["workflow_id"],
            "workflow": workflow,
            "groupId": row["group_id"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _group(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "sortOrder": row["sort_order"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "batchId": row["batch_id"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "host": row["host"],
            "workflowId": row["workflow_id"], "profileId": row["profile_id"],
            "profileName": row["profile_name"], "status": row["status"],
            "runIndex": row["run_index"], "runTotal": row["run_total"],
            "overrides": json.loads(row["overrides_json"]),
            "remoteTaskId": row["remote_task_id"],
            "results": json.loads(row["results_json"]) if row["results_json"] else [],
            "error": row["error"], "message": row["message"],
            "autoDownload": bool(row["auto_download"]),
            "sendFullWorkflow": bool(row["send_full_workflow"]),
            "cancelRequested": bool(row["cancel_requested"]),
        }

    def list_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM profiles ORDER BY updated_at DESC").fetchall()
        return [self._profile(row) for row in rows]

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        return self._profile(row) if row else None

    def list_groups(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_groups ORDER BY sort_order, name COLLATE NOCASE"
            ).fetchall()
        return [self._group(row) for row in rows]

    @staticmethod
    def _clean_name(value: Any, label: str) -> str:
        name = str(value or "").strip()
        if not name:
            raise ValueError(f"{label}不能为空")
        if len(name) > 80:
            raise ValueError(f"{label}不能超过 80 个字符")
        return name

    def create_group(self, name: Any) -> dict[str, Any]:
        group_id, stamp = uuid.uuid4().hex, now_iso()
        clean_name = self._clean_name(name, "分组名称")
        try:
            with self.connect() as conn:
                next_order = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM workflow_groups"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO workflow_groups (id,name,sort_order,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (group_id, clean_name, next_order, stamp, stamp),
                )
                row = conn.execute("SELECT * FROM workflow_groups WHERE id=?", (group_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("已存在同名分组") from exc
        assert row is not None
        return self._group(row)

    def update_group(self, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        clean_name = self._clean_name(payload.get("name"), "分组名称")
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    "UPDATE workflow_groups SET name=?, updated_at=? WHERE id=?",
                    (clean_name, now_iso(), group_id),
                )
                if cursor.rowcount == 0:
                    raise FileNotFoundError("分组不存在")
                row = conn.execute("SELECT * FROM workflow_groups WHERE id=?", (group_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError("已存在同名分组") from exc
        assert row is not None
        return self._group(row)

    def delete_group(self, group_id: str) -> None:
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM workflow_groups WHERE id=?", (group_id,)).fetchone():
                raise FileNotFoundError("分组不存在")
            conn.execute("UPDATE profiles SET group_id=NULL, updated_at=? WHERE group_id=?", (now_iso(), group_id))
            conn.execute("DELETE FROM workflow_groups WHERE id=?", (group_id,))

    def save_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        workflow = payload.get("workflow")
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("工作流 JSON 无效")
        host = str(payload.get("host", "ai"))
        if host not in HOSTS:
            raise ValueError("站点无效")
        workflow_id = str(payload.get("workflowId", "")).strip()
        if not workflow_id:
            raise ValueError("Workflow ID 不能为空")
        stamp = now_iso()
        with self.connect() as conn:
            create_new = bool(payload.get("createNew"))
            explicit_id = str(payload.get("id") or "").strip()
            old = None
            if not create_new and explicit_id:
                old = conn.execute("SELECT * FROM profiles WHERE id=?", (explicit_id,)).fetchone()
            if not create_new and not old:
                old = conn.execute(
                    "SELECT * FROM profiles WHERE host=? AND workflow_id=? ORDER BY updated_at DESC LIMIT 1",
                    (host, workflow_id),
                ).fetchone()
            profile_id = old["id"] if old else (explicit_id if explicit_id and not create_new else uuid.uuid4().hex)
            created = old["created_at"] if old else stamp
            name = old["name"] if old else self._clean_name(
                payload.get("name") or f"工作流 {workflow_id}", "工作流名称"
            )
            group_id = old["group_id"] if old else (str(payload.get("groupId") or "").strip() or None)
            if group_id and not conn.execute(
                "SELECT 1 FROM workflow_groups WHERE id=?", (group_id,)
            ).fetchone():
                raise ValueError("所选分组不存在")
            conn.execute(
                """INSERT INTO profiles
                (id,name,host,workflow_id,workflow_json,group_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, host=excluded.host, workflow_id=excluded.workflow_id,
                    workflow_json=excluded.workflow_json, group_id=excluded.group_id,
                    updated_at=excluded.updated_at""",
                (
                    profile_id, name, host, workflow_id, json_dumps(workflow), group_id, created, stamp,
                ),
            )
        result = self.get_profile(profile_id)
        assert result is not None
        return result

    def update_profile(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            current = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if not current:
                raise FileNotFoundError("工作流不存在")
            name = self._clean_name(payload.get("name", current["name"]), "工作流名称")
            group_id = payload.get("groupId", current["group_id"])
            group_id = str(group_id).strip() if group_id else None
            if group_id and not conn.execute(
                "SELECT 1 FROM workflow_groups WHERE id=?", (group_id,)
            ).fetchone():
                raise ValueError("所选分组不存在")
            conn.execute(
                "UPDATE profiles SET name=?, group_id=?, updated_at=? WHERE id=?",
                (name, group_id, now_iso(), profile_id),
            )
        result = self.get_profile(profile_id)
        assert result is not None
        return result

    def delete_profile(self, profile_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))

    def enqueue(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        runs = safe_int(payload.get("runs"), 1, 1, 200)
        host = str(payload.get("host", "ai"))
        if host not in HOSTS:
            raise ValueError("站点无效")
        workflow_id = str(payload.get("workflowId", "")).strip()
        if not workflow_id:
            raise ValueError("Workflow ID 不能为空")
        base_overrides = payload.get("overrides")
        if not isinstance(base_overrides, list):
            raise ValueError("参数列表无效")
        batch_id = uuid.uuid4().hex
        seed_mode = str(payload.get("seedMode", "random"))
        seed_step = safe_int(payload.get("seedStep"), 1, -10**12, 10**12)
        seed_value = payload.get("seedValue")
        stamp = now_iso()
        created_ids: list[str] = []
        workflow_snapshot = payload.get("workflow")
        if (not isinstance(workflow_snapshot, dict) or not workflow_snapshot) and payload.get("profileId"):
            profile = self.get_profile(str(payload.get("profileId")))
            workflow_snapshot = profile["workflow"] if profile else None
        if not isinstance(workflow_snapshot, dict) or not workflow_snapshot:
            raise ValueError("任务缺少工作流快照")
        workflow_snapshot = normalize_workflow_seeds(
            json.loads(json.dumps(workflow_snapshot, ensure_ascii=False))
        )
        with self.connect() as conn:
            for index in range(1, runs + 1):
                job_id = uuid.uuid4().hex[:12]
                overrides = json.loads(json.dumps(base_overrides, ensure_ascii=False))
                for item in overrides:
                    value = item.get("fieldValue")
                    if "seed" in str(item.get("fieldName", "")).lower() and isinstance(value, str):
                        try:
                            item["fieldValue"] = int(value)
                        except ValueError:
                            pass
                self._prepare_overrides(overrides, index, runs, seed_mode, seed_value, seed_step)
                conn.execute(
                    """INSERT INTO jobs
                    (id,batch_id,created_at,updated_at,host,workflow_id,profile_id,profile_name,
                     status,run_index,run_total,overrides_json,workflow_json,auto_download,
                     send_full_workflow,message)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id, batch_id, stamp, stamp, host, workflow_id,
                        payload.get("profileId"), str(payload.get("profileName") or "临时工作流"),
                        "QUEUED", index, runs, json_dumps(overrides), json_dumps(workflow_snapshot),
                        1 if payload.get("autoDownload", True) else 0,
                        1 if payload.get("sendFullWorkflow") else 0, "等待串行执行",
                    ),
                )
                created_ids.append(job_id)
        return [self.get_job(job_id) for job_id in created_ids if self.get_job(job_id)]

    @staticmethod
    def _prepare_overrides(
        overrides: list[dict[str, Any]], index: int, total: int,
        seed_mode: str, seed_value: Any, seed_step: int,
    ) -> None:
        seed: int | None = None
        seed_item = next((x for x in overrides if x.get("fieldName") == "seed"), None)
        base = seed_value if seed_value not in (None, "") else (seed_item or {}).get("fieldValue")
        try:
            base_int = int(base)
        except (TypeError, ValueError):
            base_int = random.SystemRandom().randrange(1, 2**63 - 1)
        if seed_mode == "random":
            seed = random.SystemRandom().randrange(1, 2**63 - 1)
        elif seed_mode == "increment":
            seed = base_int + (index - 1) * seed_step
        else:
            seed = base_int
        if seed_item is not None:
            seed_item["fieldValue"] = seed
        tokens = {
            "{{index}}": str(index), "{{total}}": str(total),
            "{{seed}}": str(seed), "{{date}}": datetime.now().strftime("%Y-%m-%d"),
        }
        for item in overrides:
            value = item.get("fieldValue")
            if isinstance(value, str):
                for token, replacement in tokens.items():
                    value = value.replace(token, replacement)
                item["fieldValue"] = value

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def restore_payload(self, job_id: str, result_index: int = 0) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise FileNotFoundError("任务不存在")
        job = self._job(row)
        results = job["results"]
        if result_index < 0 or (results and result_index >= len(results)):
            raise FileNotFoundError("结果不存在")

        workflow: dict[str, Any] | None = None
        source = ""
        if results:
            local_path = results[result_index].get("localPath")
            if local_path and Path(local_path).suffix.lower() == ".png":
                workflow = read_png_json_text(Path(local_path), "prompt")
                if workflow:
                    source = "PNG 内嵌工作流"
        if not workflow and row["workflow_json"]:
            parsed = json.loads(row["workflow_json"])
            if isinstance(parsed, dict):
                workflow, source = parsed, "任务工作流快照"
        profile = self.get_profile(str(row["profile_id"] or ""))
        if not workflow and profile:
            workflow, source = profile["workflow"], "已保存工作流 + 当次参数"
        if not workflow:
            raise ValueError("没有可恢复的工作流信息")

        workflow = json.loads(json.dumps(workflow, ensure_ascii=False))
        for item in job["overrides"]:
            node = workflow.get(str(item.get("nodeId")))
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                node["inputs"][str(item.get("fieldName"))] = item.get("fieldValue")
        seed_item = next((item for item in job["overrides"] if item.get("fieldName") == "seed"), None)
        ui_overrides = json.loads(json.dumps(job["overrides"], ensure_ascii=False))
        for item in ui_overrides:
            value = item.get("fieldValue")
            if "seed" in str(item.get("fieldName", "")).lower() and isinstance(value, int):
                item["fieldValue"] = str(value)
        browser_safe_workflow(workflow)
        return {
            "name": f"{job['profileName']} · 复用",
            "host": job["host"], "workflowId": job["workflowId"],
            "profileId": job["profileId"], "workflow": workflow,
            "overrides": ui_overrides, "source": source,
            "seed": str(seed_item.get("fieldValue")) if seed_item else None,
            "sourceJobId": job_id, "resultIndex": result_index,
        }

    def generation_info(self, job_id: str, result_index: int = 0) -> dict[str, Any]:
        restore = self.restore_payload(job_id, result_index)
        job = self.get_job(job_id)
        assert job is not None
        result = job["results"][result_index] if job["results"] else {}
        return {
            "source": restore["source"], "jobId": job_id,
            "remoteTaskId": job["remoteTaskId"], "createdAt": job["createdAt"],
            "profileName": job["profileName"], "workflowId": job["workflowId"],
            "host": job["host"], "resultType": result.get("fileType"),
            **summarize_workflow(restore["workflow"]),
            "workflow": restore["workflow"], "overrides": restore["overrides"],
        }

    def list_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, run_index DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._job(row) for row in rows]

    def next_job(self, host: str | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            host_clause = " AND host=?" if host else ""
            params: tuple[Any, ...] = (host,) if host else ()
            row = conn.execute(
                f"""SELECT * FROM jobs
                WHERE status IN ('RUNNING','QUEUED') AND cancel_requested=0
                {host_clause}
                ORDER BY CASE WHEN status='RUNNING' THEN 0 ELSE 1 END, created_at, run_index LIMIT 1""",
                params,
            ).fetchone()
        return self._job(row) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        mapping = {
            "status": "status", "remoteTaskId": "remote_task_id", "results": "results_json",
            "error": "error", "message": "message", "cancelRequested": "cancel_requested",
        }
        parts, values = [], []
        for key, value in fields.items():
            if key not in mapping:
                continue
            if key == "results":
                value = json_dumps(value)
            if key == "cancelRequested":
                value = 1 if value else 0
            parts.append(f"{mapping[key]}=?")
            values.append(value)
        if not parts:
            return
        parts.append("updated_at=?")
        values.extend([now_iso(), job_id])
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {','.join(parts)} WHERE id=?", values)

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if not job:
            return None
        if job["status"] == "QUEUED":
            self.update_job(job_id, status="CANCELLED", message="已从本地队列取消")
        elif job["status"] in ("RUNNING", "SUBMITTING"):
            self.update_job(job_id, cancelRequested=True, message="正在取消远程任务")
        return self.get_job(job_id)

    def retry(self, job_id: str) -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if not job:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            new_id = uuid.uuid4().hex[:12]
            stamp = now_iso()
            conn.execute(
                """INSERT INTO jobs
                (id,batch_id,created_at,updated_at,host,workflow_id,profile_id,profile_name,status,
                 run_index,run_total,overrides_json,workflow_json,auto_download,message)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id, uuid.uuid4().hex, stamp, stamp, row["host"], row["workflow_id"],
                    row["profile_id"], row["profile_name"], "QUEUED", 1, 1,
                    row["overrides_json"], row["workflow_json"], row["auto_download"], "重试任务待执行",
                ),
            )
        return self.get_job(new_id)

    # ------------------------------------------------------------------ #
    # Result gallery: a permanent index of produced files. It lives apart
    # from the job queue on purpose, so cleaning the queue never removes a
    # result the user already paid for.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _gallery_id(job_id: str, result_index: int) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", f"{job_id}-{result_index}")

    @staticmethod
    def _normalize_path(value: str | Path | None) -> str | None:
        """Store every path resolved so the same file is never indexed twice."""
        if not value:
            return None
        try:
            return str(Path(value).expanduser().resolve())
        except OSError:
            return str(Path(value).expanduser())

    @staticmethod
    def _gallery_file_type(result: dict[str, Any], local_path: str | None) -> str:
        raw = str(result.get("fileType") or "").strip().lower().lstrip(".")
        if raw:
            return raw
        name = local_path or str(result.get("fileUrl") or result.get("url") or "")
        return Path(urlparse(name).path).suffix.lower().lstrip(".") or "file"

    @staticmethod
    def _workflow_ref(conn: sqlite3.Connection, workflow: dict[str, Any] | None) -> str | None:
        if not isinstance(workflow, dict) or not workflow:
            return None
        payload = json_dumps(workflow)
        ref = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        conn.execute(
            "INSERT OR IGNORE INTO gallery_workflows (ref, workflow_json, created_at) VALUES (?,?,?)",
            (ref, payload, now_iso()),
        )
        return ref

    @staticmethod
    def _upsert_gallery_item(conn: sqlite3.Connection, payload: dict[str, Any],
                             only_fill_local: bool = False) -> None:
        columns = ",".join(payload)
        placeholders = ",".join("?" * len(payload))
        updates = ",".join(
            f"{key}=excluded.{key}" for key in payload if key not in ("id", "created_at")
        )
        guard = (
            " WHERE gallery_items.local_path IS NULL OR gallery_items.local_path=''"
            if only_fill_local else ""
        )
        local_path = payload.get("local_path")
        if local_path and not only_fill_local:
            # Keep a single entry per file: the archived row wins over an
            # entry that was derived from the filename alone.
            conn.execute(
                "DELETE FROM gallery_items WHERE local_path=? AND id<>?",
                (local_path, payload["id"]),
            )
        conn.execute(
            f"INSERT INTO gallery_items ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}{guard}",
            tuple(payload.values()),
        )

    @staticmethod
    def _gallery_search_text(profile_name: str, file_name: str | None, job_id: str,
                             remote_task_id: str | None, node_id: str | None,
                             overrides_json: str | None,
                             workflow: dict[str, Any] | None = None) -> str:
        """Flatten everything worth searching: parameters (incl. prompts saved
        as overrides) plus every text input inside the workflow snapshot."""
        parts: list[str] = [
            profile_name, file_name or "", job_id,
            remote_task_id or "", node_id or "",
        ]
        try:
            overrides = json.loads(overrides_json) if overrides_json else []
        except json.JSONDecodeError:
            overrides = []
        if isinstance(overrides, list):
            for override in overrides:
                if isinstance(override, dict):
                    parts.append(str(override.get("fieldName") or ""))
                    parts.append(str(override.get("fieldValue") or ""))
        if isinstance(workflow, dict):
            for node in workflow.values():
                if not isinstance(node, dict):
                    continue
                inputs = node.get("inputs")
                if not isinstance(inputs, dict):
                    continue
                for key, value in inputs.items():
                    if isinstance(value, str) and value.strip():
                        parts.append(f"{key}:{value.strip()}")
        return " ".join(part for part in parts if part).strip().lower()[:12000]

    @staticmethod
    def _gallery_item(row: sqlite3.Row) -> dict[str, Any]:
        local_path = row["local_path"] or ""
        has_local = bool(local_path) and Path(local_path).exists()
        item_id = row["id"]
        stored_search = row["search_text"] if "search_text" in row.keys() else ""
        search_text = stored_search or Database._gallery_search_text(
            row["profile_name"], row["file_name"], row["job_id"],
            row["remote_task_id"], row["node_id"], row["overrides_json"],
        )
        return {
            "id": item_id,
            "jobId": row["job_id"],
            "resultIndex": row["result_index"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "host": row["host"],
            "workflowId": row["workflow_id"],
            "profileId": row["profile_id"],
            "profileName": row["profile_name"],
            "remoteTaskId": row["remote_task_id"],
            "nodeId": row["node_id"],
            "fileType": row["file_type"],
            "fileName": row["file_name"],
            "hasLocal": has_local,
            "fileMissing": bool(local_path) and not has_local,
            "localUrl": f"/api/gallery/{item_id}/file" if has_local else "",
            "remoteUrl": row["remote_url"] or "",
            "url": f"/api/gallery/{item_id}/file" if has_local else (row["remote_url"] or ""),
            "textUrl": f"/api/gallery/{item_id}/text",
            "source": row["source"],
            "searchText": search_text,
        }

    def gallery_row(self, item_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM gallery_items WHERE id=?", (item_id,)).fetchone()

    def get_gallery_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.gallery_row(item_id)
        return self._gallery_item(row) if row else None

    def list_gallery(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM gallery_items ORDER BY created_at DESC, result_index DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._gallery_item(row) for row in rows]

    def archive_job_results(self, job_id: str) -> int:
        """Copy the results of a successful job into the permanent gallery."""
        with self.connect() as conn:
            return self._archive_job(conn, job_id)

    def _archive_job(self, conn: sqlite3.Connection, job_id: str) -> int:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "SUCCESS":
            return 0
        try:
            results = json.loads(row["results_json"]) if row["results_json"] else []
        except json.JSONDecodeError:
            results = []
        if not results:
            return 0
        workflow = self._load_json(row["workflow_json"])
        if not isinstance(workflow, dict) or not workflow:
            profile_id = row["profile_id"]
            if profile_id:
                profile_row = conn.execute(
                    "SELECT workflow_json FROM profiles WHERE id=?", (profile_id,)
                ).fetchone()
                workflow = self._load_json(profile_row["workflow_json"]) if profile_row else None
        ref = self._workflow_ref(conn, workflow if isinstance(workflow, dict) else None)
        stamp = now_iso()
        count = 0
        for index, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            local_path = self._normalize_path(str(item.get("localPath") or ""))
            remote = str(item.get("fileUrl") or item.get("url") or "")
            if not local_path and not remote:
                continue
            node_id = str(item.get("nodeId") or "") or None
            self._upsert_gallery_item(conn, {
                "id": self._gallery_id(row["id"], index),
                "job_id": row["id"],
                "result_index": index,
                "created_at": row["created_at"],
                "updated_at": stamp,
                "host": row["host"],
                "workflow_id": row["workflow_id"],
                "profile_id": row["profile_id"],
                "profile_name": row["profile_name"],
                "remote_task_id": row["remote_task_id"],
                "node_id": node_id,
                "file_type": self._gallery_file_type(item, local_path),
                "file_name": (
                    Path(local_path).name if local_path
                    else (Path(urlparse(remote).path).name or None)
                ),
                "local_path": local_path,
                "remote_url": remote or None,
                "result_json": json_dumps(item),
                "overrides_json": row["overrides_json"],
                "search_text": self._gallery_search_text(
                    row["profile_name"], Path(local_path).name if local_path
                    else (Path(urlparse(remote).path).name or None),
                    row["id"], row["remote_task_id"], node_id,
                    row["overrides_json"], workflow if isinstance(workflow, dict) else None,
                ),
                "workflow_ref": ref,
                "source": "任务归档",
            })
            count += 1
        return count

    def import_local_files(self, settings: dict[str, Any] | None = None) -> int:
        """Index files that already live in the download folder (backfill)."""
        config = settings if isinstance(settings, dict) else DEFAULT_SETTINGS
        root = Path(str(config.get("download_dir") or DEFAULT_SETTINGS["download_dir"])).expanduser()
        if not root.is_dir():
            return 0
        count = 0
        with self.connect() as conn:
            profiles = {
                row["id"]: row["name"]
                for row in conn.execute("SELECT id, name FROM profiles").fetchall()
            }
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path.name.endswith(".workflow.json"):
                    continue
                if self._import_local_file(conn, path, root, profiles):
                    count += 1
        return count

    def _import_local_file(
        self, conn: sqlite3.Connection, path: Path, root: Path, profiles: dict[str, str],
    ) -> bool:
        match = re.fullmatch(r"([0-9a-fA-F]{6,32})_(\d+)(\.[^.]+)?", path.name)
        if match:
            job_id, index_text = match.group(1).lower(), int(match.group(2))
        else:
            job_id = "local-" + hashlib.sha1(
                str(path.relative_to(root)).encode("utf-8")
            ).hexdigest()[:10]
            index_text = 1
        result_index = max(0, index_text - 1)
        path = Path(self._normalize_path(path) or path)
        already = conn.execute(
            "SELECT id FROM gallery_items WHERE local_path=?", (str(path),)
        ).fetchone()
        if already:
            return False
        sidecar = path.with_name(path.name + ".workflow.json")
        meta = self._load_json(sidecar.read_text("utf-8")) if sidecar.is_file() else None
        if not isinstance(meta, dict):
            meta = {}
        workflow = meta.get("workflow") if isinstance(meta.get("workflow"), dict) else None
        source_job = str(meta.get("sourceJobId") or job_id)
        try:
            result_index = int(meta.get("resultIndex", result_index))
        except (TypeError, ValueError):
            pass
        profile_id = str(meta.get("profileId") or "") or None
        profile_name = profiles.get(profile_id or "", "")
        if not profile_name:
            profile_name = str(meta.get("name") or "").replace(" · 复用", "").strip()
        if not profile_name:
            profile_name = "本地结果"
        try:
            created_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(
                timespec="seconds"
            )
        except OSError:
            created_at = now_iso()
        ref = self._workflow_ref(conn, workflow)
        self._upsert_gallery_item(conn, {
            "id": self._gallery_id(source_job, result_index),
            "job_id": source_job,
            "result_index": result_index,
            "created_at": created_at,
            "updated_at": now_iso(),
            "host": str(meta.get("host") or "ai"),
            "workflow_id": str(meta.get("workflowId") or ""),
            "profile_id": profile_id,
            "profile_name": profile_name,
            "remote_task_id": None,
            "node_id": None,
            "file_type": path.suffix.lower().lstrip(".") or "file",
            "file_name": path.name,
            "local_path": str(path),
            "remote_url": None,
            "result_json": json_dumps({"localPath": str(path), "localUrl": ""}),
            "overrides_json": json_dumps(meta.get("overrides") or []),
            "search_text": self._gallery_search_text(
                profile_name, path.name, source_job, None, None,
                json_dumps(meta.get("overrides") or []), workflow,
            ),
            "workflow_ref": ref,
            "source": "本地文件导入",
        }, only_fill_local=True)
        return True

    def set_gallery_local_path(self, item_id: str, local_path: str) -> None:
        resolved = self._normalize_path(local_path) or local_path
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM gallery_items WHERE local_path=? AND id<>?", (resolved, item_id)
            )
            conn.execute(
                "UPDATE gallery_items SET local_path=?, file_name=?, updated_at=? WHERE id=?",
                (resolved, Path(resolved).name, now_iso(), item_id),
            )

    def forget_gallery_item(self, item_id: str) -> bool:
        """Remove an entry from the gallery index. Files on disk are kept."""
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM gallery_items WHERE id=?", (item_id,))
            return cur.rowcount > 0

    def gallery_file_target(self, item_id: str) -> Path:
        row = self.gallery_row(item_id)
        if not row:
            raise FileNotFoundError("结果不存在")
        local_path = row["local_path"]
        if not local_path:
            raise FileNotFoundError("该结果尚未保存到本地")
        target = Path(local_path).resolve()
        allowed = Path(SETTINGS.load()["download_dir"]).expanduser().resolve()
        if allowed not in target.parents or not target.is_file():
            raise FileNotFoundError("结果文件不存在")
        return target

    def _gallery_workflow(self, row: sqlite3.Row) -> tuple[dict[str, Any] | None, str]:
        ref = row["workflow_ref"]
        if ref:
            with self.connect() as conn:
                stored = conn.execute(
                    "SELECT workflow_json FROM gallery_workflows WHERE ref=?", (ref,)
                ).fetchone()
            if stored:
                workflow = self._load_json(stored["workflow_json"])
                if isinstance(workflow, dict) and workflow:
                    return workflow, "图库归档工作流"
        local_path = row["local_path"]
        if local_path:
            path = Path(local_path)
            sidecar = path.with_name(path.name + ".workflow.json")
            if sidecar.is_file():
                meta = self._load_json(sidecar.read_text("utf-8"))
                if isinstance(meta, dict) and isinstance(meta.get("workflow"), dict):
                    return meta["workflow"], "本地 sidecar 工作流"
            if path.suffix.lower() == ".png":
                workflow = read_png_json_text(path, "prompt")
                if isinstance(workflow, dict) and workflow:
                    return workflow, "PNG 内嵌工作流"
        job = self.get_job(row["job_id"])
        if job:
            with self.connect() as conn:
                stored = conn.execute(
                    "SELECT workflow_json FROM jobs WHERE id=?", (row["job_id"],)
                ).fetchone()
            if stored:
                workflow = self._load_json(stored["workflow_json"])
                if isinstance(workflow, dict) and workflow:
                    return workflow, "任务工作流快照"
            profile = self.get_profile(str(row["profile_id"] or ""))
            if profile:
                return profile["workflow"], "已保存工作流 + 当次参数"
        return None, ""

    def gallery_restore_payload(self, item_id: str) -> dict[str, Any]:
        row = self.gallery_row(item_id)
        if not row:
            raise FileNotFoundError("结果不存在")
        workflow, source = self._gallery_workflow(row)
        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("这个结果没有留下可复用的工作流信息")
        workflow = json.loads(json.dumps(workflow, ensure_ascii=False))
        try:
            overrides = json.loads(row["overrides_json"]) if row["overrides_json"] else []
        except json.JSONDecodeError:
            overrides = []
        for item in overrides:
            node = workflow.get(str(item.get("nodeId")))
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                node["inputs"][str(item.get("fieldName"))] = item.get("fieldValue")
        seed_item = next((item for item in overrides if item.get("fieldName") == "seed"), None)
        ui_overrides = json.loads(json.dumps(overrides, ensure_ascii=False))
        for item in ui_overrides:
            value = item.get("fieldValue")
            if "seed" in str(item.get("fieldName", "")).lower() and isinstance(value, int):
                item["fieldValue"] = str(value)
        browser_safe_workflow(workflow)
        return {
            "name": f"{row['profile_name']} · 复用",
            "host": row["host"],
            "workflowId": row["workflow_id"],
            "profileId": row["profile_id"],
            "workflow": workflow,
            "overrides": ui_overrides,
            "source": source or "结果图库",
            "seed": str(seed_item.get("fieldValue")) if seed_item else None,
            "sourceJobId": row["job_id"],
            "resultIndex": row["result_index"],
        }

    def gallery_generation_info(self, item_id: str) -> dict[str, Any]:
        restore = self.gallery_restore_payload(item_id)
        row = self.gallery_row(item_id)
        assert row is not None
        return {
            "source": restore["source"],
            "jobId": row["job_id"],
            "remoteTaskId": row["remote_task_id"],
            "createdAt": row["created_at"],
            "profileName": row["profile_name"],
            "workflowId": row["workflow_id"],
            "host": row["host"],
            "resultType": row["file_type"],
            **summarize_workflow(restore["workflow"]),
            "workflow": restore["workflow"],
            "overrides": restore["overrides"],
        }

    @staticmethod
    def _load_json(raw: str | None) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def clear_finished(self) -> int:
        """Drop finished jobs from the queue. Results stay in the gallery.

        Anything still queued, submitting, running or being cancelled is kept,
        and every finished job is archived into the gallery beforehand so no
        produced file is ever lost by this action.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status IN ('SUCCESS','FAILED','CANCELLED')"
            ).fetchall()
            for row in rows:
                try:
                    self._archive_job(conn, row["id"])
                except Exception:  # archiving must never block the cleanup
                    pass
            with ACTIVE_JOB_LOCK:
                active = sorted(ACTIVE_JOB_IDS)
            placeholders = ",".join("?" * len(active)) if active else "''"
            finished = ",".join(f"'{status}'" for status in FINISHED_JOB_STATUSES)
            not_active = ",".join(f"'{status}'" for status in ACTIVE_JOB_STATUSES)
            cur = conn.execute(
                f"""DELETE FROM jobs
                    WHERE status IN ({finished})
                      AND status NOT IN ({not_active})
                      AND NOT (status='RUNNING' AND cancel_requested=1)
                      AND id NOT IN ({placeholders})""",
                tuple(active),
            )
            return cur.rowcount


class RunningHubClient:
    def __init__(self, host_key: str, api_key: str):
        if host_key not in HOSTS:
            raise ValueError("未知站点")
        self.host_key = host_key
        self.host = HOSTS[host_key]
        self.api_key = api_key
        self.base = f"https://{self.host}"

    def _post_json(self, path: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
        data = json_dumps(payload).encode("utf-8")
        request = Request(
            self.base + path,
            data=data,
            method="POST",
            headers={
                "Host": self.host,
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "RunningHub-Desk/1.0",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = None
            raise ApiError(f"HTTP {exc.code}: {raw[:300]}", code=exc.code, payload=body) from exc
        except (URLError, TimeoutError) as exc:
            raise ApiError(f"网络请求失败：{exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"RunningHub 返回了非 JSON 内容：{raw[:300]}") from exc

    @staticmethod
    def _require_success(body: dict[str, Any]) -> dict[str, Any]:
        if body.get("code") not in (0, 200):
            raise ApiError(str(body.get("msg") or "RunningHub API 错误"), code=body.get("code"), payload=body)
        return body

    def account_status(self) -> dict[str, Any]:
        body = self._post_json("/uc/openapi/accountStatus", {"apikey": self.api_key})
        return self._require_success(body)

    def fetch_workflow(self, workflow_id: str) -> dict[str, Any]:
        body = self._post_json(
            "/api/openapi/getJsonApiFormat",
            {"apiKey": self.api_key, "workflowId": workflow_id},
        )
        self._require_success(body)
        prompt = (body.get("data") or {}).get("prompt")
        if isinstance(prompt, str):
            try:
                return json.loads(prompt)
            except json.JSONDecodeError as exc:
                raise ApiError("远程工作流 JSON 解析失败") from exc
        if isinstance(prompt, dict):
            return prompt
        raise ApiError("远程响应中没有工作流 prompt")

    def submit(self, workflow_id: str, overrides: list[dict[str, Any]], workflow: dict | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "apiKey": self.api_key,
            "workflowId": workflow_id,
            "nodeInfoList": overrides,
            "addMetadata": True,
        }
        if workflow:
            payload["workflow"] = json_dumps(workflow)
        body = self._post_json("/task/openapi/create", payload, timeout=120)
        self._require_success(body)
        task_id = str((body.get("data") or {}).get("taskId") or "")
        if not task_id:
            raise ApiError("任务提交成功响应中没有 taskId", payload=body)
        return body

    def outputs(self, task_id: str) -> dict[str, Any]:
        return self._post_json(
            "/task/openapi/outputs", {"apiKey": self.api_key, "taskId": task_id}, timeout=60
        )

    def cancel(self, task_id: str) -> dict[str, Any]:
        body = self._post_json(
            "/task/openapi/cancel", {"apiKey": self.api_key, "taskId": task_id}, timeout=60
        )
        return self._require_success(body)

    def upload(self, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        boundary = f"----RunningHubDesk{uuid.uuid4().hex}"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"apiKey\"\r\n\r\n{self.api_key}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"fileType\"\r\n\r\ninput\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{sanitize_filename(filename)}\"\r\nContent-Type: {content_type}\r\n\r\n"
            ).encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        request = Request(
            self.base + "/task/openapi/upload", data=b"".join(parts), method="POST",
            headers={
                "Host": self.host, "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json", "User-Agent": "RunningHub-Desk/1.0",
            },
        )
        try:
            with urlopen(request, timeout=180) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ApiError(f"文件上传失败：{exc}") from exc
        return self._require_success(body)


class QueueWorker(threading.Thread):
    def __init__(self, db: Database, settings: SettingsStore, keys: KeyStore, host: str | None = None):
        super().__init__(name=f"runninghub-queue-{host or 'all'}", daemon=True)
        self.db = db
        self.settings = settings
        self.keys = keys
        self.host = host
        self.wake = threading.Event()
        self.stop_event = threading.Event()

    def notify(self) -> None:
        self.wake.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            config = self.settings.load()
            if config.get("queue_paused"):
                self.wake.wait(2)
                self.wake.clear()
                continue
            job = self.db.next_job(self.host)
            if not job:
                self.wake.wait(2)
                self.wake.clear()
                continue
            try:
                with ACTIVE_JOB_LOCK:
                    ACTIVE_JOB_IDS.add(job["id"])
                self._process(job)
            except Exception as exc:  # keep the worker alive
                self.db.update_job(job["id"], status="FAILED", error=str(exc), message="任务处理异常")
            finally:
                with ACTIVE_JOB_LOCK:
                    ACTIVE_JOB_IDS.discard(job["id"])

    def _process(self, job: dict[str, Any]) -> None:
        api_key = self.keys.get(job["host"])
        if not api_key:
            self.db.update_job(job["id"], status="FAILED", error="该站点尚未保存 API Key", message="缺少密钥")
            return
        client = RunningHubClient(job["host"], api_key)
        task_id = job.get("remoteTaskId")
        if not task_id:
            self.db.update_job(job["id"], status="SUBMITTING", message="正在提交 RunningHub")
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT workflow_json, send_full_workflow FROM jobs WHERE id=?", (job["id"],)
                ).fetchone()
            workflow = (
                json.loads(row["workflow_json"])
                if row and row["workflow_json"] and row["send_full_workflow"] else None
            )
            response = client.submit(job["workflowId"], job["overrides"], workflow)
            data = response.get("data") or {}
            task_id = str(data.get("taskId"))
            initial = str(data.get("taskStatus") or "RUNNING")
            self.db.update_job(
                job["id"], status="RUNNING", remoteTaskId=task_id,
                message=f"RunningHub {initial.lower()} · {task_id}",
            )

        config = self.settings.load()
        interval = safe_int(config.get("poll_interval"), 5, 2, 60)
        deadline = time.monotonic() + safe_int(config.get("timeout_minutes"), 60, 5, 1440) * 60
        consecutive_errors = 0
        while time.monotonic() < deadline and not self.stop_event.is_set():
            current = self.db.get_job(job["id"])
            if not current:
                return
            if current.get("cancelRequested"):
                try:
                    client.cancel(task_id)
                    message = "RunningHub 远程任务已取消"
                except ApiError as exc:
                    message = f"本地已停止；远程取消返回：{exc}"
                self.db.update_job(job["id"], status="CANCELLED", message=message)
                return
            body = client.outputs(task_id)
            code = body.get("code")
            code_text = str(code) if code is not None else ""
            data = body.get("data")
            if code in (0, 200) and isinstance(data, list):
                if not data:
                    self.db.update_job(job["id"], status="SUCCESS", results=[], message="任务完成，无文件输出")
                    return
                results = [dict(item) for item in data if isinstance(item, dict)]
                self.db.update_job(job["id"], status="SUCCESS", results=results, message="任务完成")
                self.db.archive_job_results(job["id"])
                if current.get("autoDownload"):
                    self.download_results(job["id"])
                return
            if code_text in TRANSIENT_OUTPUT_CODES:
                consecutive_errors = 0
                self.db.update_job(
                    job["id"], status="RUNNING", error=None,
                    message="RunningHub 正在执行 · 等待结果",
                )
                self.stop_event.wait(interval)
                continue
            if code not in (0, 200, None):
                reason = body.get("msg") or "RunningHub 任务失败"
                if isinstance(data, dict) and data.get("failedReason"):
                    reason = json_dumps(data.get("failedReason"))
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    self.db.update_job(job["id"], status="FAILED", error=str(reason), message=f"API 错误 {code}")
                    return
                self.db.update_job(
                    job["id"], status="RUNNING", error=str(reason),
                    message=f"查询暂时异常（{code}）· 将重试 {consecutive_errors}/3",
                )
                self.stop_event.wait(interval)
                continue
            consecutive_errors = 0
            status = "运行中"
            if isinstance(data, dict):
                status = str(data.get("taskStatus") or data.get("status") or status)
            self.db.update_job(job["id"], message=f"RunningHub {status}")
            self.stop_event.wait(interval)
        self.db.update_job(job["id"], status="FAILED", error="等待结果超时", message="已停止轮询")

    def download_results(self, job_id: str) -> list[dict[str, Any]]:
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError("任务不存在")
        config = self.settings.load()
        root = Path(config["download_dir"]).expanduser().resolve()
        day_dir = root / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        results = job.get("results") or []
        for index, item in enumerate(results, start=1):
            if item.get("localPath") and Path(item["localPath"]).exists():
                target = Path(item["localPath"])
                try:
                    restore = self.db.restore_payload(job_id, index - 1)
                    sidecar = target.with_name(target.name + ".workflow.json")
                    sidecar.write_text(
                        json.dumps(restore, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    item["workflowSidecar"] = str(sidecar)
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    item["sidecarError"] = str(exc)
                continue
            url = str(item.get("fileUrl") or item.get("url") or "")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                continue
            raw_name = Path(parsed.path).name
            ext = Path(raw_name).suffix or ("." + str(item.get("fileType") or "bin").lstrip("."))
            filename = sanitize_filename(f"{job_id}_{index}{ext}")
            target = day_dir / filename
            request = Request(url, headers={"User-Agent": "RunningHub-Desk/1.0"})
            try:
                with urlopen(request, timeout=300) as response, target.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
                item["localPath"] = str(target)
                item["localUrl"] = f"/api/jobs/{quote(job_id)}/result/{index - 1}"
                self.db.update_job(job_id, results=results)
                try:
                    restore = self.db.restore_payload(job_id, index - 1)
                    sidecar = target.with_name(target.name + ".workflow.json")
                    sidecar.write_text(
                        json.dumps(restore, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    item["workflowSidecar"] = str(sidecar)
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    item["sidecarError"] = str(exc)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                item["downloadError"] = str(exc)
        self.db.update_job(job_id, results=results, message="任务完成 · 结果已保存")
        self.db.archive_job_results(job_id)
        return results

    def save_gallery_item(self, item_id: str) -> dict[str, Any]:
        """Fetch a gallery result that is still only stored remotely."""
        row = self.db.gallery_row(item_id)
        if not row:
            raise FileNotFoundError("结果不存在")
        local_path = row["local_path"]
        if local_path and Path(local_path).exists():
            return self.db.get_gallery_item(item_id) or {}
        url = str(row["remote_url"] or "")
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("这个结果没有可下载的远程地址")
        config = self.settings.load()
        root = Path(config["download_dir"]).expanduser().resolve()
        day_dir = root / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(urlparse(url).path).suffix
        ext = suffix or ("." + str(row["file_type"] or "bin").lstrip("."))
        target = day_dir / sanitize_filename(f"{row['job_id']}_{row['result_index'] + 1}{ext}")
        request = Request(url, headers={"User-Agent": "RunningHub-Desk/1.0"})
        try:
            with urlopen(request, timeout=300) as response, target.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"下载失败：{exc}") from exc
        self.db.set_gallery_local_path(item_id, str(target))
        try:
            restore = self.db.gallery_restore_payload(item_id)
            sidecar = target.with_name(target.name + ".workflow.json")
            sidecar.write_text(json.dumps(restore, ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, OSError, json.JSONDecodeError):
            pass
        return self.db.get_gallery_item(item_id) or {}


class QueueCoordinator:
    """Run one serial worker per account/host, allowing different hosts in parallel."""

    def __init__(self, db: Database, settings: SettingsStore, keys: KeyStore):
        self.db = db
        self.workers = {
            host: QueueWorker(db, settings, keys, host=host) for host in HOSTS
        }

    def start(self) -> None:
        for worker in self.workers.values():
            worker.start()

    def notify(self) -> None:
        for worker in self.workers.values():
            worker.notify()

    def stop(self) -> None:
        for worker in self.workers.values():
            worker.stop_event.set()
            worker.notify()

    def download_results(self, job_id: str) -> list[dict[str, Any]]:
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError("任务不存在")
        return self.workers[job["host"]].download_results(job_id)

    def save_gallery_item(self, item_id: str) -> dict[str, Any]:
        row = self.db.gallery_row(item_id)
        if not row:
            raise FileNotFoundError("结果不存在")
        host = row["host"] if row["host"] in self.workers else "ai"
        return self.workers[host].save_gallery_item(item_id)


SETTINGS = SettingsStore()
KEYS = KeyStore()
DB = Database()
WORKER = QueueCoordinator(DB, SETTINGS, KEYS)



def bootstrap_gallery() -> None:
    """Index existing downloads, unless bootstrap is disabled (e.g. in tests)."""
    if os.environ.get("RHW_SKIP_BOOTSTRAP") == "1":
        return
    try:
        imported = DB.import_local_files(SETTINGS.load())
        if imported:
            print(f"结果图库：已索引 {imported} 个本地文件")
    except Exception as exc:  # never block startup because of a re-scan
        print(f"结果图库扫描失败：{exc}")


bootstrap_gallery()


def client_for(host_key: str) -> RunningHubClient:
    api_key = KEYS.get(host_key)
    if not api_key:
        raise ValueError(f"{HOSTS.get(host_key, host_key)} 尚未保存 API Key")
    return RunningHubClient(host_key, api_key)


class Handler(BaseHTTPRequestHandler):
    server_version = "RunningHubDesk/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _json_body(self, max_size: int = 25 * 1024 * 1024) -> dict[str, Any]:
        length = safe_int(self.headers.get("Content-Length"), 0, 0, max_size + 1)
        if length > max_size:
            raise ValueError("请求内容过大")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON 请求无效") from exc
        if not isinstance(body, dict):
            raise ValueError("请求必须是 JSON 对象")
        return body

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: Exception, status: int = 400) -> None:
        payload: dict[str, Any] = {"ok": False, "error": str(exc)}
        if isinstance(exc, ApiError):
            payload["code"] = exc.code
            payload["details"] = exc.payload
        self._send_json(payload, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self._send_json({"ok": True, "time": now_iso()})
                return
            if path == "/api/state":
                jobs = DB.list_jobs(200)
                self._send_json({
                    "ok": True,
                    "hosts": [{"id": key, "domain": value, "hasKey": bool(KEYS.get(key))} for key, value in HOSTS.items()],
                    "keyStore": KEYS.backend_name,
                    "settings": SETTINGS.load(),
                    "groups": DB.list_groups(),
                    "profiles": DB.list_profiles(),
                    "jobs": jobs,
                    "summary": {
                        "queued": sum(j["status"] == "QUEUED" for j in jobs),
                        "running": sum(j["status"] in ("RUNNING", "SUBMITTING") for j in jobs),
                        "success": sum(j["status"] == "SUCCESS" for j in jobs),
                        "failed": sum(j["status"] == "FAILED" for j in jobs),
                    },
                })
                return
            if path == "/api/profiles":
                self._send_json({"ok": True, "groups": DB.list_groups(), "profiles": DB.list_profiles()})
                return
            if path == "/api/jobs":
                limit = safe_int(parse_qs(parsed.query).get("limit", [200])[0], 200, 1, 1000)
                self._send_json({"ok": True, "jobs": DB.list_jobs(limit)})
                return
            if path == "/api/media-preview":
                query = parse_qs(parsed.query)
                self._serve_media_preview(
                    query.get("host", [""])[0], query.get("name", [""])[0]
                )
                return
            if path == "/api/gallery":
                limit = safe_int(parse_qs(parsed.query).get("limit", [500])[0], 500, 1, 2000)
                self._send_json({"ok": True, "items": DB.list_gallery(limit)})
                return
            match = re.fullmatch(r"/api/gallery/([\w-]+)/file", path)
            if match:
                self._send_local_file(DB.gallery_file_target(match.group(1)))
                return
            match = re.fullmatch(r"/api/gallery/([\w-]+)/text", path)
            if match:
                self._serve_gallery_text(match.group(1))
                return
            match = re.fullmatch(r"/api/gallery/([\w-]+)/restore", path)
            if match:
                self._send_json({"ok": True, "restore": DB.gallery_restore_payload(match.group(1))})
                return
            match = re.fullmatch(r"/api/gallery/([\w-]+)/metadata", path)
            if match:
                self._send_json({"ok": True, "metadata": DB.gallery_generation_info(match.group(1))})
                return
            match = re.fullmatch(r"/api/jobs/([\w-]+)/result/(\d+)", path)
            if match:
                self._serve_result(match.group(1), int(match.group(2)))
                return
            match = re.fullmatch(r"/api/jobs/([\w-]+)/result/(\d+)/text", path)
            if match:
                self._serve_text_result(match.group(1), int(match.group(2)))
                return
            match = re.fullmatch(r"/api/jobs/([\w-]+)/restore", path)
            if match:
                result_index = safe_int(
                    parse_qs(parsed.query).get("result", [0])[0], 0, 0, 1000
                )
                self._send_json({"ok": True, "restore": DB.restore_payload(match.group(1), result_index)})
                return
            match = re.fullmatch(r"/api/jobs/([\w-]+)/metadata", path)
            if match:
                result_index = safe_int(
                    parse_qs(parsed.query).get("result", [0])[0], 0, 0, 1000
                )
                self._send_json({"ok": True, "metadata": DB.generation_info(match.group(1), result_index)})
                return
            self._serve_static(path)
        except Exception as exc:
            self._error(exc, 404 if isinstance(exc, FileNotFoundError) else 400)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._json_body(70 * 1024 * 1024)
            if path == "/api/key":
                host = str(body.get("host", ""))
                KEYS.set(host, str(body.get("apiKey", "")))
                self._send_json({"ok": True, "host": host, "hasKey": True, "keyStore": KEYS.backend_name})
                return
            if path == "/api/key/test":
                host = str(body.get("host", ""))
                result = client_for(host).account_status()
                self._send_json({"ok": True, "data": result.get("data")})
                return
            if path == "/api/settings":
                settings = SETTINGS.update(body)
                WORKER.notify()
                self._send_json({"ok": True, "settings": settings})
                return
            if path == "/api/profiles/import":
                profile = DB.save_profile(body)
                self._send_json({"ok": True, "profile": profile})
                return
            if path == "/api/profiles/remote":
                host = str(body.get("host", "ai"))
                workflow_id = str(body.get("workflowId", "")).strip()
                workflow = client_for(host).fetch_workflow(workflow_id)
                profile = DB.save_profile({**body, "workflow": workflow})
                self._send_json({"ok": True, "profile": profile, "nodeCount": len(workflow)})
                return
            if path == "/api/groups":
                group = DB.create_group(body.get("name"))
                self._send_json({"ok": True, "group": group}, 201)
                return
            if path == "/api/gallery/scan":
                count = DB.import_local_files(SETTINGS.load())
                self._send_json({"ok": True, "count": count, "items": DB.list_gallery(500)})
                return
            match = re.fullmatch(r"/api/gallery/([\w-]+)/save", path)
            if match:
                item = WORKER.save_gallery_item(match.group(1))
                self._send_json({"ok": True, "item": item})
                return
            if path == "/api/queue":
                jobs = DB.enqueue(body)
                WORKER.notify()
                self._send_json({"ok": True, "jobs": jobs, "count": len(jobs)})
                return
            if path == "/api/queue/pause":
                settings = SETTINGS.update({"queue_paused": True})
                self._send_json({"ok": True, "settings": settings})
                return
            if path == "/api/queue/resume":
                settings = SETTINGS.update({"queue_paused": False})
                WORKER.notify()
                self._send_json({"ok": True, "settings": settings})
                return
            if path == "/api/upload":
                host = str(body.get("host", "ai"))
                encoded = str(body.get("dataBase64", ""))
                if "," in encoded:
                    encoded = encoded.split(",", 1)[1]
                content = base64.b64decode(encoded, validate=True)
                if len(content) > 64 * 1024 * 1024:
                    raise ValueError("文件不能超过 64MB")
                result = client_for(host).upload(
                    str(body.get("filename") or "upload.bin"), content,
                    str(body.get("contentType") or "application/octet-stream"),
                )
                remote_name = str(
                    (result.get("data") or {}).get("fileName")
                    or (result.get("data") or {}).get("filename") or ""
                )
                if remote_name and str(body.get("contentType") or "").startswith("image/"):
                    preview = media_preview_path(host, remote_name)
                    preview.parent.mkdir(parents=True, exist_ok=True)
                    preview.write_bytes(content)
                    result["data"]["previewUrl"] = (
                        f"/api/media-preview?host={quote(host)}&name={quote(remote_name)}"
                    )
                self._send_json({"ok": True, "data": result.get("data")})
                return
            if path == "/api/open-downloads":
                target = Path(SETTINGS.load()["download_dir"]).resolve()
                target.mkdir(parents=True, exist_ok=True)
                if platform.system() == "Darwin":
                    subprocess.Popen(["open", str(target)])
                elif platform.system() == "Windows":
                    os.startfile(str(target))  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", str(target)])
                self._send_json({"ok": True, "path": str(target)})
                return
            match = re.fullmatch(r"/api/jobs/([\w-]+)/(cancel|retry|download)", path)
            if match:
                job_id, action = match.groups()
                if action == "cancel":
                    job = DB.request_cancel(job_id)
                elif action == "retry":
                    job = DB.retry(job_id)
                    WORKER.notify()
                else:
                    results = WORKER.download_results(job_id)
                    job = DB.get_job(job_id)
                    if job:
                        job["results"] = results
                if not job:
                    raise FileNotFoundError("任务不存在")
                self._send_json({"ok": True, "job": job})
                return
            raise FileNotFoundError("接口不存在")
        except Exception as exc:
            self._error(exc, 404 if isinstance(exc, FileNotFoundError) else 400)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._json_body()
            match = re.fullmatch(r"/api/profiles/([\w-]+)", path)
            if match:
                profile = DB.update_profile(match.group(1), body)
                self._send_json({"ok": True, "profile": profile})
                return
            match = re.fullmatch(r"/api/groups/([\w-]+)", path)
            if match:
                group = DB.update_group(match.group(1), body)
                self._send_json({"ok": True, "group": group})
                return
            raise FileNotFoundError("接口不存在")
        except Exception as exc:
            self._error(exc, 404 if isinstance(exc, FileNotFoundError) else 400)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/key":
                host = parse_qs(parsed.query).get("host", [""])[0]
                KEYS.delete(host)
                self._send_json({"ok": True})
                return
            match = re.fullmatch(r"/api/profiles/([\w-]+)", path)
            if match:
                DB.delete_profile(match.group(1))
                self._send_json({"ok": True})
                return
            match = re.fullmatch(r"/api/groups/([\w-]+)", path)
            if match:
                DB.delete_group(match.group(1))
                self._send_json({"ok": True})
                return
            if path == "/api/jobs/finished":
                count = DB.clear_finished()
                self._send_json({"ok": True, "count": count, "gallery": len(DB.list_gallery(2000))})
                return
            match = re.fullmatch(r"/api/gallery/([\w-]+)", path)
            if match:
                DB.forget_gallery_item(match.group(1))
                self._send_json({"ok": True})
                return
            raise FileNotFoundError("接口不存在")
        except Exception as exc:
            self._error(exc, 404 if isinstance(exc, FileNotFoundError) else 400)

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents and target != STATIC_ROOT.resolve():
            raise FileNotFoundError("文件不存在")
        if not target.exists() or not target.is_file():
            target = STATIC_ROOT / "index.html"
        data = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _serve_result(self, job_id: str, index: int) -> None:
        job = DB.get_job(job_id)
        if not job or index < 0 or index >= len(job["results"]):
            raise FileNotFoundError("结果不存在")
        local_path = job["results"][index].get("localPath")
        if not local_path:
            raise FileNotFoundError("结果尚未保存到本地")
        target = Path(local_path).resolve()
        allowed = Path(SETTINGS.load()["download_dir"]).expanduser().resolve()
        if allowed not in target.parents or not target.is_file():
            raise FileNotFoundError("结果文件不存在")
        self._send_local_file(target)

    def _send_local_file(self, target: Path) -> None:
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(target.name)}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _read_result_text(self, local_path: str | None, remote_url: str) -> str:
        if local_path:
            target = Path(local_path).resolve()
            allowed = Path(SETTINGS.load()["download_dir"]).expanduser().resolve()
            if allowed not in target.parents or not target.is_file():
                raise FileNotFoundError("文本结果文件不存在")
            data = target.read_bytes()
        else:
            if urlparse(remote_url).scheme not in ("http", "https"):
                raise FileNotFoundError("文本结果没有可读取的地址")
            request = Request(remote_url, headers={"User-Agent": "RunningHub-Desk/1.0"})
            try:
                with urlopen(request, timeout=60) as response:
                    data = response.read(MAX_TEXT_PREVIEW_BYTES + 1)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                raise ValueError(f"读取远程文本失败：{exc}") from exc
        return decode_text_preview(data)

    def _serve_gallery_text(self, item_id: str) -> None:
        row = DB.gallery_row(item_id)
        if not row:
            raise FileNotFoundError("结果不存在")
        self._send_json({"ok": True, "text": self._read_result_text(row["local_path"], row["remote_url"] or "")})

    def _serve_text_result(self, job_id: str, index: int) -> None:
        job = DB.get_job(job_id)
        if not job or index < 0 or index >= len(job["results"]):
            raise FileNotFoundError("结果不存在")
        result = job["results"][index]
        text = self._read_result_text(
            result.get("localPath"), str(result.get("fileUrl") or result.get("url") or "")
        )
        self._send_json({"ok": True, "text": text})

    def _serve_media_preview(self, host: str, remote_name: str) -> None:
        if host not in HOSTS or not remote_name:
            raise FileNotFoundError("预览不存在")
        target = media_preview_path(host, remote_name)
        if not target.is_file():
            raise FileNotFoundError("暂无本地预览")
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RunningHub 本地 API 工作台")
    parser.add_argument("--port", type=int, default=8765, help="本地端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not STATIC_ROOT.exists():
        raise SystemExit(f"缺少静态文件目录：{STATIC_ROOT}")
    WORKER.start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"RunningHub Desk 已启动：{url}")
    print("按 Control-C 停止。任务历史会自动保留。")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        WORKER.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
