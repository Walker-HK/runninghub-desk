import json
import os
import struct
import tempfile
import unittest
from pathlib import Path


TEST_ROOT = tempfile.mkdtemp(prefix="runninghub-desk-tests-")
os.environ["RHW_DATA_DIR"] = TEST_ROOT
os.environ["RHW_FORCE_FILE_KEYS"] = "1"
os.environ["RHW_SKIP_BOOTSTRAP"] = "1"

import app  # noqa: E402


SAMPLE_WORKFLOW = {
    "1": {
        "inputs": {"text": "scene {{index}} / {{seed}}", "clip": ["2", 0]},
        "class_type": "CLIPTextEncode",
        "_meta": {"title": "Prompt"},
    },
    "3": {
        "inputs": {"seed": 100, "steps": 8, "cfg": 1.0, "positive": ["1", 0]},
        "class_type": "KSampler",
        "_meta": {"title": "Sampler"},
    },
}


class RunningHubDeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rhd-case-")
        app.DATA_ROOT = Path(self.temp.name)
        app.DB_PATH = app.DATA_ROOT / "runninghub.db"
        app.SETTINGS_PATH = app.DATA_ROOT / "settings.json"
        app.FALLBACK_KEYS_PATH = app.DATA_ROOT / ".keys.json"
        self.settings = app.SettingsStore()
        self.keys = app.KeyStore()
        self.db = app.Database()

    def tearDown(self):
        self.temp.cleanup()

    def test_keys_are_separate_per_domain(self):
        self.keys.set("ai", "ai-secret")
        self.keys.set("cn", "cn-secret")
        self.assertEqual(self.keys.get("ai"), "ai-secret")
        self.assertEqual(self.keys.get("cn"), "cn-secret")
        self.keys.delete("ai")
        self.assertIsNone(self.keys.get("ai"))
        self.assertEqual(self.keys.get("cn"), "cn-secret")

    def test_text_preview_decodes_utf8_bom_and_utf16(self):
        self.assertEqual(app.decode_text_preview(b"\xef\xbb\xbfhello \xe4\xb8\x96\xe7\x95\x8c"), "hello 世界")
        self.assertEqual(app.decode_text_preview("反推提示词".encode("utf-16")), "反推提示词")

    def test_text_preview_rejects_oversized_content(self):
        with self.assertRaisesRegex(ValueError, "超过 4MB"):
            app.decode_text_preview(b"x" * (app.MAX_TEXT_PREVIEW_BYTES + 1))

    def test_profile_and_incrementing_queue(self):
        profile = self.db.save_profile({
            "name": "Test", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        jobs = self.db.enqueue({
            "host": "ai", "workflowId": "123", "profileId": profile["id"],
            "profileName": profile["name"], "runs": 3, "seedMode": "increment",
            "seedValue": 100, "seedStep": 5, "autoDownload": False,
            "overrides": [
                {"nodeId": "1", "fieldName": "text", "fieldValue": "scene {{index}} / {{seed}}"},
                {"nodeId": "3", "fieldName": "seed", "fieldValue": 100},
            ],
        })
        self.assertEqual(len(jobs), 3)
        seeds = [next(x["fieldValue"] for x in job["overrides"] if x["fieldName"] == "seed") for job in jobs]
        prompts = [next(x["fieldValue"] for x in job["overrides"] if x["fieldName"] == "text") for job in jobs]
        self.assertEqual(seeds, [100, 105, 110])
        self.assertEqual(prompts, ["scene 1 / 100", "scene 2 / 105", "scene 3 / 110"])

    def test_each_host_has_an_independent_serial_queue(self):
        ai_profile = self.db.save_profile({
            "name": "AI", "host": "ai", "workflowId": "ai-1", "workflow": SAMPLE_WORKFLOW,
        })
        cn_profile = self.db.save_profile({
            "name": "CN", "host": "cn", "workflowId": "cn-1", "workflow": SAMPLE_WORKFLOW,
        })
        for host, profile in (("ai", ai_profile), ("cn", cn_profile)):
            self.db.enqueue({
                "host": host, "workflowId": profile["workflowId"], "profileId": profile["id"],
                "profileName": profile["name"], "runs": 2, "seedMode": "fixed",
                "seedValue": 100, "autoDownload": False, "overrides": [],
            })

        self.assertEqual(self.db.next_job("ai")["host"], "ai")
        self.assertEqual(self.db.next_job("ai")["runIndex"], 1)
        self.assertEqual(self.db.next_job("cn")["host"], "cn")
        self.assertEqual(self.db.next_job("cn")["runIndex"], 1)

        coordinator = app.QueueCoordinator(self.db, self.settings, self.keys)
        self.assertEqual(set(coordinator.workers), {"ai", "cn"})
        self.assertEqual(coordinator.workers["ai"].host, "ai")
        self.assertEqual(coordinator.workers["cn"].host, "cn")

    def test_profile_can_be_renamed_and_grouped(self):
        profile = self.db.save_profile({
            "name": "Original", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        group = self.db.create_group("人像")
        updated = self.db.update_profile(profile["id"], {"name": "Portrait V2", "groupId": group["id"]})
        self.assertEqual(updated["name"], "Portrait V2")
        self.assertEqual(updated["groupId"], group["id"])

        renamed_group = self.db.update_group(group["id"], {"name": "人物"})
        self.assertEqual(renamed_group["name"], "人物")
        self.db.delete_group(group["id"])
        self.assertIsNone(self.db.get_profile(profile["id"])["groupId"])

    def test_importing_same_workflow_updates_in_place_and_preserves_metadata(self):
        profile = self.db.save_profile({
            "name": "My Name", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        group = self.db.create_group("收藏")
        self.db.update_profile(profile["id"], {"groupId": group["id"]})
        changed_workflow = {**SAMPLE_WORKFLOW, "9": {"inputs": {"value": 1}, "class_type": "Test"}}
        refreshed = self.db.save_profile({
            "name": "工作流 123", "host": "ai", "workflowId": "123", "workflow": changed_workflow,
        })
        self.assertEqual(refreshed["id"], profile["id"])
        self.assertEqual(refreshed["name"], "My Name")
        self.assertEqual(refreshed["groupId"], group["id"])
        self.assertEqual(len(self.db.list_profiles()), 1)
        self.assertIn("9", refreshed["workflow"])

    def test_remote_fetch_can_save_duplicate_as_new_profile(self):
        original = self.db.save_profile({
            "name": "Original", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        duplicate = self.db.save_profile({
            "name": "Original copy", "host": "ai", "workflowId": "123",
            "workflow": SAMPLE_WORKFLOW, "createNew": True,
        })
        self.assertNotEqual(duplicate["id"], original["id"])
        self.assertEqual(duplicate["name"], "Original copy")
        self.assertEqual(len(self.db.list_profiles()), 2)

    def test_remote_fetch_overwrites_explicit_profile_but_preserves_name_and_group(self):
        original = self.db.save_profile({
            "name": "Keep this name", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        group = self.db.create_group("收藏")
        self.db.update_profile(original["id"], {"groupId": group["id"]})
        changed = {**SAMPLE_WORKFLOW, "99": {"inputs": {"value": 1}, "class_type": "Test"}}
        overwritten = self.db.save_profile({
            "id": original["id"], "name": "Ignored", "host": "cn",
            "workflowId": "456", "workflow": changed,
        })
        self.assertEqual(overwritten["id"], original["id"])
        self.assertEqual(overwritten["name"], "Keep this name")
        self.assertEqual(overwritten["groupId"], group["id"])
        self.assertEqual(overwritten["host"], "cn")
        self.assertEqual(overwritten["workflowId"], "456")
        self.assertIn("99", overwritten["workflow"])

    def test_worker_submits_polls_and_finishes(self):
        self.keys.set("ai", "secret")
        profile = self.db.save_profile({
            "name": "Test", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        [job] = self.db.enqueue({
            "host": "ai", "workflowId": "123", "profileId": profile["id"],
            "profileName": profile["name"], "runs": 1, "seedMode": "fixed",
            "seedValue": 100, "autoDownload": False,
            "overrides": [{"nodeId": "3", "fieldName": "seed", "fieldValue": 100}],
        })

        original_client = app.RunningHubClient

        class FakeClient:
            def __init__(self, host, key):
                self.host, self.key = host, key

            def submit(self, workflow_id, overrides, workflow):
                self.assertions = (workflow_id, overrides, workflow)
                return {"code": 0, "data": {"taskId": "remote-1", "taskStatus": "RUNNING"}}

            def outputs(self, task_id):
                return {"code": 0, "data": [{"fileUrl": "https://example.invalid/out.png", "fileType": "png"}]}

        app.RunningHubClient = FakeClient
        try:
            worker = app.QueueWorker(self.db, self.settings, self.keys)
            worker._process(job)
        finally:
            app.RunningHubClient = original_client

        finished = self.db.get_job(job["id"])
        self.assertEqual(finished["status"], "SUCCESS")
        self.assertEqual(finished["remoteTaskId"], "remote-1")
        self.assertEqual(finished["results"][0]["fileType"], "png")

    def test_804_is_treated_as_running_then_succeeds(self):
        self.keys.set("ai", "secret")
        profile = self.db.save_profile({
            "name": "Test", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        [job] = self.db.enqueue({
            "host": "ai", "workflowId": "123", "profileId": profile["id"],
            "profileName": profile["name"], "runs": 1, "seedMode": "fixed",
            "seedValue": 100, "autoDownload": False,
            "overrides": [{"nodeId": "3", "fieldName": "seed", "fieldValue": 100}],
        })
        original_client = app.RunningHubClient

        class FakeClient:
            calls = 0

            def __init__(self, host, key):
                pass

            def submit(self, workflow_id, overrides, workflow):
                return {"code": 0, "data": {"taskId": "remote-804", "taskStatus": "RUNNING"}}

            def outputs(self, task_id):
                self.__class__.calls += 1
                if self.__class__.calls == 1:
                    return {"code": 804, "msg": "APIKEY_TASK_IS_RUNNING", "data": None}
                return {"code": 0, "msg": "success", "data": [{"fileUrl": "https://example.invalid/out.png", "fileType": "png"}]}

        class NoDelayEvent:
            def is_set(self):
                return False

            def wait(self, timeout=None):
                return False

        app.RunningHubClient = FakeClient
        try:
            worker = app.QueueWorker(self.db, self.settings, self.keys)
            worker.stop_event = NoDelayEvent()
            worker._process(job)
        finally:
            app.RunningHubClient = original_client

        finished = self.db.get_job(job["id"])
        self.assertEqual(FakeClient.calls, 2)
        self.assertEqual(finished["status"], "SUCCESS")
        self.assertIsNone(finished["error"])

    def test_legacy_804_failure_is_recovered_on_database_open(self):
        with self.db.connect() as conn:
            stamp = app.now_iso()
            conn.execute(
                """INSERT INTO jobs
                (id,batch_id,created_at,updated_at,host,workflow_id,profile_name,status,
                 run_index,run_total,overrides_json,remote_task_id,error,auto_download,message)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("legacy", "batch", stamp, stamp, "ai", "123", "Test", "FAILED", 1, 1,
                 "[]", "remote-old", "APIKEY_TASK_IS_RUNNING", 0, "API 错误 804"),
            )
        reopened = app.Database()
        recovered = reopened.get_job("legacy")
        self.assertEqual(recovered["status"], "RUNNING")
        self.assertIsNone(recovered["error"])

    def test_connections_are_not_editable_scalars(self):
        # Mirrors the frontend rule used to keep node wiring out of nodeInfoList.
        scalars = []
        for node_id, node in SAMPLE_WORKFLOW.items():
            for name, value in node["inputs"].items():
                if value is None or isinstance(value, (str, int, float, bool)):
                    scalars.append((node_id, name))
        self.assertNotIn(("1", "clip"), scalars)
        self.assertNotIn(("3", "positive"), scalars)
        self.assertIn(("3", "seed"), scalars)

    def test_media_preview_path_is_safe_and_stable(self):
        first = app.media_preview_path("ai", "api/folder/example.png")
        second = app.media_preview_path("ai", "api/folder/example.png")
        different_host = app.media_preview_path("cn", "api/folder/example.png")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different_host)
        self.assertEqual(first.parent, app.DATA_ROOT / "media-previews")
        self.assertEqual(first.suffix, ".png")
        self.assertNotIn("folder", first.name)

    def test_job_keeps_workflow_snapshot_for_parameter_restore(self):
        profile = self.db.save_profile({
            "name": "Snapshot", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        [job] = self.db.enqueue({
            "host": "ai", "workflowId": "123", "profileId": profile["id"],
            "profileName": profile["name"], "runs": 1, "seedMode": "fixed",
            "seedValue": 321, "autoDownload": False,
            "overrides": [{"nodeId": "3", "fieldName": "seed", "fieldValue": 321}],
        })
        restored = self.db.restore_payload(job["id"])
        self.assertEqual(restored["source"], "任务工作流快照")
        self.assertEqual(restored["workflow"]["3"]["inputs"]["seed"], 321)
        self.assertEqual(restored["seed"], "321")

    def test_restore_prefers_embedded_png_prompt(self):
        profile = self.db.save_profile({
            "name": "Embedded", "host": "ai", "workflowId": "123", "workflow": SAMPLE_WORKFLOW,
        })
        [job] = self.db.enqueue({
            "host": "ai", "workflowId": "123", "profileId": profile["id"],
            "profileName": profile["name"], "runs": 1, "seedMode": "fixed",
            "seedValue": 444, "autoDownload": False,
            "overrides": [{"nodeId": "3", "fieldName": "seed", "fieldValue": 444}],
        })
        embedded = {**SAMPLE_WORKFLOW, "8": {"inputs": {"marker": "from-png"}, "class_type": "Marker"}}
        text = b"prompt\0" + json.dumps(embedded).encode()
        png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", len(text), b"tEXt") + text + b"\0\0\0\0"
        png += struct.pack(">I4s", 0, b"IEND") + b"\0\0\0\0"
        path = Path(self.temp.name) / "result.png"
        path.write_bytes(png)
        self.db.update_job(job["id"], results=[{"fileType": "png", "localPath": str(path)}])
        restored = self.db.restore_payload(job["id"])
        self.assertEqual(restored["source"], "PNG 内嵌工作流")
        self.assertEqual(restored["workflow"]["8"]["inputs"]["marker"], "from-png")
        self.assertEqual(restored["workflow"]["3"]["inputs"]["seed"], 444)

    def test_large_seed_round_trips_without_javascript_precision_loss(self):
        large_seed = 2_320_430_639_408_748_563
        workflow = json.loads(json.dumps(SAMPLE_WORKFLOW))
        workflow["3"]["inputs"]["seed"] = large_seed
        profile = self.db.save_profile({
            "name": "Large seed", "host": "ai", "workflowId": "123", "workflow": workflow,
        })
        self.assertEqual(profile["workflow"]["3"]["inputs"]["seed"], str(large_seed))
        [job] = self.db.enqueue({
            "host": "ai", "workflowId": "123", "profileId": profile["id"],
            "profileName": profile["name"], "runs": 1, "seedMode": "fixed",
            "seedValue": str(large_seed), "autoDownload": False,
            "workflow": profile["workflow"],
            "overrides": [{"nodeId": "3", "fieldName": "seed", "fieldValue": str(large_seed)}],
        })
        with self.db.connect() as conn:
            row = conn.execute("SELECT workflow_json, overrides_json FROM jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(json.loads(row["workflow_json"])["3"]["inputs"]["seed"], large_seed)
        self.assertEqual(json.loads(row["overrides_json"])[0]["fieldValue"], large_seed)
        restored = self.db.restore_payload(job["id"])
        self.assertEqual(restored["seed"], str(large_seed))
        self.assertEqual(restored["overrides"][0]["fieldValue"], str(large_seed))

    def test_workflow_summary_extracts_prompt_models_loras_and_parameters(self):
        workflow = {
            "1": {"class_type": "CLIPTextEncode", "_meta": {"title": "Prompt"}, "inputs": {"text": "a city"}},
            "2": {"class_type": "UNETLoader", "_meta": {"title": "Load Model"}, "inputs": {"unet_name": "model.safetensors"}},
            "3": {"class_type": "LoraLoaderModelOnly", "_meta": {"title": "Load LoRA"}, "inputs": {"lora_name": "detail.safetensors", "strength_model": 0.8}},
            "4": {"class_type": "KSampler", "inputs": {"seed": "123", "steps": 20, "cfg": 4.5, "model": ["2", 0]}},
            "5": {"class_type": "PrimitiveFloat", "_meta": {"title": "Float (duration)"}, "inputs": {"value": 8}},
        }
        summary = app.summarize_workflow(workflow)
        self.assertEqual(summary["prompts"][0]["value"], "a city")
        self.assertEqual(summary["models"][0]["value"], "model.safetensors")
        self.assertEqual(summary["loras"][0]["name"], "detail.safetensors")
        self.assertEqual(summary["loras"][0]["strengthModel"], 0.8)
        names = {item["name"] for item in summary["parameters"]}
        self.assertTrue({"seed", "steps", "cfg", "duration"}.issubset(names))


if __name__ == "__main__":
    unittest.main()
