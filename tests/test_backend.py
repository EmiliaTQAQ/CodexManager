import json
import os
import tempfile
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

    def start_turn(self, prompt, cwd, requested_thread_id=None, job_id=None):
        thread_id = requested_thread_id or "thread-1"
        self.calls.append((prompt, cwd, thread_id, job_id))
        return {"thread_id": thread_id, "turn_id": "turn-%s" % len(self.calls)}

    def interrupt(self, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))

    def restart(self):
        self.restart_count += 1


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
        self.service = ManagerService(home=self.root, data_root=self.root / "app-data")
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

    def test_chat_job_uses_app_server_events(self):
        started = self.service.start_chat("检查代码", str(self.root))
        self.assertEqual(started["thread_id"], "thread-1")
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

    def test_followup_reuses_thread_and_stop_interrupts_turn(self):
        first = self.service.start_chat("第一条", str(self.root))
        second = self.service.start_chat("第二条", str(self.root), first["thread_id"])
        self.assertEqual(second["thread_id"], first["thread_id"])
        self.assertEqual(self.fake_app_server.calls[1][2], "thread-1")
        self.service.stop_chat(second["job_id"])
        self.assertEqual(self.fake_app_server.interrupts[-1], ("thread-1", "turn-2"))

    def test_chat_uses_configured_workspace_when_cwd_omitted(self):
        started = self.service.start_chat("检查代码")
        self.assertEqual(self.fake_app_server.calls[-1][1], self.service.workspace)

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


if __name__ == "__main__":
    unittest.main()
