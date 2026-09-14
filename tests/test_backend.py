import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.service import (
    ManagerService,
    codex_event_text,
    normalize_app_server_event,
    parse_codex_event,
    parse_simple_toml,
)


class FakeAppServer:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []
        self.interrupts = []
        self.restart_count = 0

    def start_turn(self, prompt, cwd, requested_thread_id=None, job_id=None, runtime_workspace_roots=None):
        thread_id = requested_thread_id or "thread-1"
        self.calls.append((prompt, cwd, thread_id, job_id, runtime_workspace_roots))
        return {"thread_id": thread_id, "turn_id": "turn-%s" % len(self.calls)}

    def interrupt(self, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))

    def restart(self):
        self.restart_count += 1

    def warmup(self):
        self.warmed = True


class ManagerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        os.environ["CODEX_MANAGER_SECRET_MODE"] = "file"
        codex = self.root / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text(
            'model = "old-model"\n'
            'model_provider = "old_provider"\n'
            'preferred_auth_method = "apikey"\n\n'
            '[model_providers.old_provider]\n'
            'name = "Old Provider"\n'
            'base_url = "https://old.example/v1"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        (codex / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}), encoding="utf-8")
        (codex / "models.json").write_text(
            json.dumps({"models": [{"slug": "old-model", "display_name": "Old Model"}]}),
            encoding="utf-8",
        )
        self.service = ManagerService(
            home=self.root,
            data_root=self.root / "app-data",
            prewarm_app_server=False,
        )
        self.fake_app_server = FakeAppServer(self.service._handle_app_server_event)
        self.service._app_server = self.fake_app_server

    def tearDown(self):
        os.environ.pop("CODEX_MANAGER_SECRET_MODE", None)
        self.temp_dir.cleanup()

    def provider_payload(self, provider_id="new_provider"):
        return {
            "id": provider_id,
            "name": "New Provider",
            "base_url": "https://new.example/v1",
            "wire_api": "responses",
            "default_model": "new-model",
            "enabled_models": ["new-model"],
            "model_catalog": [{"slug": "new-model", "display_name": "New Model", "context_window": 128000}],
        }

    def wait_for_app_server_call(self, count=1):
        deadline = time.time() + 2
        while time.time() < deadline:
            if len(self.fake_app_server.calls) >= count:
                return self.fake_app_server.calls[count - 1]
            time.sleep(0.01)
        self.fail("后台 Codex turn 未启动")

    def test_parse_simple_toml_reads_top_level_and_provider(self):
        parsed = parse_simple_toml('[model_providers.demo]\nname = "Demo"\n')
        self.assertEqual(parsed["model_providers.demo"]["name"], "Demo")

    def test_parse_codex_events_and_extract_agent_text(self):
        event = parse_codex_event(
            '{"type":"item.completed","item":{"type":"agent_message","text":"完成了"}}'
        )
        self.assertEqual(codex_event_text(event), "完成了")
        self.assertIsNone(parse_codex_event("not-json"))

    def test_normalize_app_server_events(self):
        event = normalize_app_server_event(
            {
                "method": "agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "完成"},
            }
        )
        self.assertEqual(event["type"], "agent_message_delta")
        self.assertEqual(event["delta"], "完成")
        item_event = normalize_app_server_event(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "实时"},
            }
        )
        self.assertEqual(item_event["type"], "agent_message_delta")
        self.assertEqual(item_event["delta"], "实时")
        command = normalize_app_server_event(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"id": "item-1", "type": "commandExecution", "command": "pwd"},
                    "startedAtMs": 1,
                },
            }
        )
        self.assertEqual(command["item"]["type"], "command_execution")
        item_command = normalize_app_server_event(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"threadId": "thread-1", "delta": "实时命令输出"},
            }
        )
        self.assertEqual(item_command["type"], "command_execution_output_delta")
        self.assertEqual(item_command["delta"], "实时命令输出")

    def test_chat_job_uses_app_server_events(self):
        started = self.service.start_chat("检查代码", str(self.root))
        self.assertIsNone(started["thread_id"])
        self.service._handle_app_server_event(
            {"type": "turn.started", "threadId": "thread-1", "_job_id": started["job_id"]}
        )
        self.service._handle_app_server_event(
            {
                "type": "agent_message_delta",
                "threadId": "thread-1",
                "delta": "已完成",
                "_job_id": started["job_id"],
            }
        )
        self.service._handle_app_server_event(
            {
                "type": "turn.completed",
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed", "error": None},
                "_job_id": started["job_id"],
            }
        )
        result = self.service.poll_chat(started["job_id"])
        self.assertTrue(result["done"])
        self.assertEqual(result["answer"], "已完成")
        self.assertEqual(result["thread_id"], "thread-1")

    def test_chat_poll_supports_incremental_event_and_answer_cursors(self):
        started = self.service.start_chat("流式输出", str(self.root))
        job_id = started["job_id"]
        self.service._handle_app_server_event(
            {
                "type": "agent_message_delta",
                "threadId": "thread-1",
                "delta": "第一段",
                "_job_id": job_id,
            }
        )

        first = self.service.poll_chat(job_id, 0, 0)
        self.assertEqual([event["type"] for event in first["events"]], ["agent_message_delta"])
        self.assertEqual(first["answer_delta"], "第一段")
        self.assertEqual(first["next_event_cursor"], 1)
        self.assertEqual(first["next_answer_cursor"], len("第一段"))
        self.assertNotIn("answer", first)

        self.service._handle_app_server_event(
            {
                "type": "agent_message_delta",
                "threadId": "thread-1",
                "delta": "第二段",
                "_job_id": job_id,
            }
        )
        second = self.service.poll_chat(
            job_id,
            first["next_event_cursor"],
            first["next_answer_cursor"],
        )
        self.assertEqual([event["delta"] for event in second["events"]], ["第二段"])
        self.assertEqual(second["answer_delta"], "第二段")

        empty = self.service.poll_chat(
            job_id,
            second["next_event_cursor"],
            second["next_answer_cursor"],
        )
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["answer_delta"], "")

    def test_chat_poll_rejects_invalid_incremental_cursor(self):
        started = self.service.start_chat("检查游标", str(self.root))
        with self.assertRaises(ValueError):
            self.service.poll_chat(started["job_id"], "invalid", 0)

    def test_start_chat_returns_before_slow_turn_start_finishes(self):
        class SlowAppServer(FakeAppServer):
            def __init__(self, callback):
                super().__init__(callback)
                self.started = False
                self.release = threading.Event()

            def start_turn(self, *args, **kwargs):
                self.started = True
                self.release.wait(2)
                return super().start_turn(*args, **kwargs)

        self.service._app_server = SlowAppServer(self.service._handle_app_server_event)
        started = self.service.start_chat("持续生成", str(self.root))
        self.assertTrue(started["job_id"])
        self.assertIsNone(started["thread_id"])
        self.assertIsNone(started["turn_id"])
        deadline = time.time() + 2
        while not self.service._app_server.started and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.service._app_server.started)
        self.service._app_server.release.set()

    def test_bridge_can_minimize_window(self):
        from backend.service import Bridge

        class FakeWindow:
            def __init__(self):
                self.minimized = False

            def minimize(self):
                self.minimized = True

        window = FakeWindow()
        bridge = Bridge(self.service)
        bridge.set_window(window)
        self.assertEqual(bridge.minimize_window(), {"minimized": True})
        self.assertTrue(window.minimized)

    def test_followup_reuses_thread_and_stop_interrupts_turn(self):
        first = self.service.start_chat("第一条", str(self.root))
        first_call = self.wait_for_app_server_call()
        second = self.service.start_chat("第二条", str(self.root), first_call[2])
        second_call = self.wait_for_app_server_call(2)
        self.assertEqual(second_call[2], first_call[2])
        self.assertEqual(self.fake_app_server.calls[1][2], "thread-1")
        self.service.stop_chat(second["job_id"])
        self.assertEqual(self.fake_app_server.interrupts[-1], ("thread-1", "turn-2"))

    def test_chat_uses_configured_workspace_when_cwd_omitted(self):
        started = self.service.start_chat("检查代码")
        call = self.wait_for_app_server_call()
        self.assertEqual(call[1], self.service.workspace)
        self.assertEqual(call[4], [self.service.workspace])

    def test_workspace_roots_are_persisted_and_exposed_to_chat(self):
        extra = self.root / "Desktop"
        extra.mkdir()
        info = self.service.add_workspace_root(str(extra))
        self.assertEqual(info["workspace_roots"], [str(self.service.workspace), str(extra.resolve())])
        self.service.start_chat("写入桌面")
        call = self.wait_for_app_server_call()
        self.assertEqual(call[4], [self.service.workspace, extra.resolve()])
        reloaded = ManagerService(home=self.root, data_root=self.root / "app-data", prewarm_app_server=False)
        self.assertEqual([str(path) for path in reloaded.workspace_roots], info["workspace_roots"])

    def test_desktop_request_auto_enables_desktop_and_adds_target_hint(self):
        desktop = self.root / "Desktop"
        desktop.mkdir()

        started = self.service.start_chat("请写一个贪吃蛇游戏放到桌面上")

        call = self.wait_for_app_server_call()
        self.assertIn(desktop.resolve(), call[4])
        self.assertIn(str(desktop.resolve()), call[0])
        self.assertIn(desktop.resolve(), self.service.workspace_roots)
        self.assertIsNone(started["thread_id"])

    def test_app_server_can_be_prewarmed_before_first_message(self):
        self.service._warm_app_server()
        self.assertTrue(self.fake_app_server.warmed)

    def test_bootstrap_discovers_provider_without_exposing_secret(self):
        result = self.service.bootstrap()
        self.assertEqual(result["status"]["provider_id"], "old_provider")
        self.assertTrue(result["providers"])
        self.assertNotIn("experimental_bearer_token", result["providers"][0])

    def test_activate_writes_files_and_can_rollback(self):
        provider = self.service.save_provider(self.provider_payload())
        self.service.set_secret(provider["id"], "test-secret-value")
        self.service._request_models = lambda _: [{"slug": "new-model", "display_name": "New Model"}]

        activated = self.service.activate_provider("new_provider")
        self.assertEqual(activated["status"], "succeeded")
        config = (self.root / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model = "new-model"', config)
        self.assertIn('model_provider = "new_provider"', config)
        auth = json.loads((self.root / ".codex" / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(auth["OPENAI_API_KEY"], "test-secret-value")

        rolled_back = self.service.rollback()
        self.assertEqual(rolled_back["status"], "succeeded")
        self.assertEqual(self.service.status()["provider_id"], "old_provider")

    def test_failed_activation_restores_previous_configuration(self):
        provider = self.service.save_provider(self.provider_payload())
        self.service.set_secret(provider["id"], "test-secret-value")
        self.service._request_models = lambda _: (_ for _ in ()).throw(ValueError("upstream failed"))

        result = self.service.activate_provider("new_provider")
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.service.status()["provider_id"], "old_provider")
        self.assertIn('model = "old-model"', (self.root / ".codex" / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(self.service._find("new_provider")["default_model"], "new-model")

    def test_activation_allows_local_provider_model_alias(self):
        provider = self.service.save_provider(self.provider_payload())
        self.service.set_secret(provider["id"], "test-secret-value")
        self.service._request_models = lambda _: [{"slug": "different-model", "display_name": "Different"}]

        result = self.service.activate_provider("new_provider", "new-model")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.service._find("new_provider")["default_model"], "new-model")
        self.assertEqual(self.fake_app_server.restart_count, 1)
        self.assertIn('model = "new-model"', (self.root / ".codex" / "config.toml").read_text(encoding="utf-8"))

    def test_activation_merges_provider_catalog_without_dropping_aliases(self):
        provider = self.service.save_provider({
            **self.provider_payload(),
            "enabled_models": ["new-model", "stale-model"],
            "model_catalog": [
                {"slug": "new-model", "display_name": "Old New"},
                {"slug": "stale-model", "display_name": "Stale"},
            ],
        })
        self.service.set_secret(provider["id"], "test-secret-value")
        self.service._request_models = lambda _: [
            {"slug": "new-model", "display_name": "New Model", "context_window": 128000},
        ]

        result = self.service.activate_provider("new_provider")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.service._find("new_provider")["enabled_models"], ["new-model", "stale-model"])
        models = json.loads((self.root / ".codex" / "models.json").read_text(encoding="utf-8"))
        self.assertEqual([item["slug"] for item in models["models"]], ["new-model", "stale-model"])


if __name__ == "__main__":
    unittest.main()
