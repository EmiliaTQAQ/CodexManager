import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.service as service_module
from backend.service import ManagerService, codex_event_text, parse_codex_event, parse_simple_toml


class FakeProcess:
    def __init__(self):
        self.stdout = iter([
            '{"type":"thread.started","thread_id":"thread-1"}\n',
            '{"type":"item.completed","item":{"type":"agent_message","text":"已完成"}}\n',
        ])
        self.stderr = tempfile.SpooledTemporaryFile(mode="w+")
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


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

    def test_chat_command_uses_json_events_and_selected_workspace(self):
        command = self.service._build_chat_command("检查代码", self.root)
        self.assertTrue(command[0].endswith("/codex") or command[0] == "codex")
        self.assertIn("--json", command)
        self.assertIn("--color", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[-2:], [str(self.root), "检查代码"])

    def test_chat_job_polls_json_events_until_complete(self):
        with patch.object(service_module.subprocess, "Popen", return_value=FakeProcess()):
            started = self.service.start_chat("检查代码", str(self.root))
        result = None
        for _ in range(20):
            result = self.service.poll_chat(started["job_id"])
            if result["done"]:
                break
            time.sleep(0.01)
        self.assertTrue(result["done"])
        self.assertEqual(result["answer"], "已完成")
        self.assertEqual(result["thread_id"], "thread-1")

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
