from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import atexit
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional


WIRE_APIS = {"responses", "chat"}
PROVIDER_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def parse_codex_event(line: str) -> Optional[Dict[str, Any]]:
    """Parse one JSONL event emitted by `codex exec --json`."""
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def codex_event_text(event: Dict[str, Any]) -> str:
    """Extract human-readable assistant text from a Codex event."""
    item = event.get("item") if isinstance(event.get("item"), dict) else event
    item_type = item.get("type")
    if item_type not in (None, "agent_message", "message", "output_text"):
        return ""
    text = item.get("text")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    output = event.get("output_text")
    return output if isinstance(output, str) else ""


def normalize_app_server_event(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate app-server notifications into the UI's small event vocabulary."""
    method = payload.get("method")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    if not isinstance(method, str):
        return None

    event: Dict[str, Any] = {"type": method.replace("/", "."), **params}
    item = params.get("item")
    if isinstance(item, dict):
        normalized_item = dict(item)
        item_type = normalized_item.get("type")
        type_map = {
            "agentMessage": "agent_message",
            "commandExecution": "command_execution",
            "userMessage": "user_message",
            "fileChange": "file_change",
            "mcpToolCall": "mcp_tool_call",
        }
        normalized_item["type"] = type_map.get(item_type, item_type)
        if normalized_item.get("aggregatedOutput"):
            normalized_item["command_output"] = normalized_item["aggregatedOutput"]
        event["item"] = normalized_item
    if method == "agentMessage/delta":
        event["type"] = "agent_message_delta"
    elif method == "commandExecution/outputDelta":
        event["type"] = "command_execution_output_delta"
        event["item"] = {"type": "command_execution", "command_output": params.get("delta", "")}
    elif method == "commandExecution/summaryTextDelta":
        event["type"] = "command_execution_summary_delta"
    elif method == "server/diagnostics/updated":
        return None
    return event


class CodexAppServer:
    """One persistent JSON-RPC connection to `codex app-server --stdio`."""

    def __init__(self, on_event: Callable[[Dict[str, Any]], None]):
        self._on_event = on_event
        self._lifecycle_lock = RLock()
        self._write_lock = RLock()
        self._pending_lock = RLock()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._next_request_id = 1
        self._process: Optional[subprocess.Popen] = None
        self._initialized = False
        self._threads: Dict[str, str] = {}
        self._active_threads: Dict[str, bool] = {}
        self._active_job_ids: Dict[str, str] = {}
        self._stderr_tail = ""

    @staticmethod
    def executable() -> str:
        executable = os.getenv("CODEX_CLI") or shutil.which("codex")
        if not executable:
            for candidate in (
                Path.home() / ".local" / "bin" / "codex",
                Path.home() / ".local" / "node" / "bin" / "codex",
                Path("/opt/homebrew/bin/codex"),
                Path("/usr/local/bin/codex"),
            ):
                if candidate.is_file():
                    executable = str(candidate)
                    break
        return executable or "codex"

    def _start_locked(self) -> None:
        process = subprocess.Popen(
            [self.executable(), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._process = process
        self._initialized = False
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._request_raw(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-manager",
                    "title": "Codex Manager",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            ensure_started=False,
        )
        self._send_notification({"method": "initialized"})
        self._initialized = True

    def _ensure_started(self) -> None:
        with self._lifecycle_lock:
            if self._process is not None and self._process.poll() is None and self._initialized:
                return
            if self._process is not None:
                self._terminate_locked()
            try:
                self._start_locked()
            except Exception:
                self._terminate_locked()
                raise

    def _send_notification(self, payload: Dict[str, Any]) -> None:
        with self._write_lock:
            process = self._process
            if process is None or process.stdin is None:
                raise ValueError("Codex app-server 未运行")
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _request_raw(self, method: str, params: Dict[str, Any], ensure_started: bool = True) -> Any:
        if ensure_started:
            self._ensure_started()
        with self._pending_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            pending = {"event": threading.Event(), "result": None, "error": None}
            self._pending[request_id] = pending
        try:
            self._send_notification({"id": request_id, "method": method, "params": params})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if not pending["event"].wait(timeout=45):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise ValueError("Codex app-server 请求超时：%s" % method)
        if pending["error"]:
            error = pending["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ValueError(message or ("Codex app-server 请求失败：%s" % method))
        return pending["result"]

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for raw_line in process.stdout:
                payload = parse_codex_event(raw_line)
                if payload is None:
                    continue
                if "id" in payload and ("result" in payload or "error" in payload):
                    with self._pending_lock:
                        pending = self._pending.pop(int(payload["id"]), None)
                    if pending:
                        pending["result"] = payload.get("result")
                        pending["error"] = payload.get("error")
                        pending["event"].set()
                    continue
                event = normalize_app_server_event(payload)
                if event is None:
                    continue
                if event.get("type") == "thread.started":
                    thread = event.get("thread")
                    if isinstance(thread, dict) and thread.get("id"):
                        self._threads[str(thread["id"])] = str(thread.get("cwd") or "")
                thread_id = event.get("threadId") or event.get("thread_id")
                if thread_id and str(thread_id) in self._active_job_ids:
                    event["_job_id"] = self._active_job_ids[str(thread_id)]
                if thread_id and event.get("type") == "turn.completed":
                    self._active_threads.pop(str(thread_id), None)
                    self._active_job_ids.pop(str(thread_id), None)
                self._on_event(event)
        except (OSError, ValueError) as exc:
            self._notify_server_error(str(exc))
        finally:
            if process.poll() is None:
                return
            self._notify_server_error(self._stderr_tail or "Codex app-server 已退出")

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                self._stderr_tail = (self._stderr_tail + line)[-4000:]
        except OSError:
            return

    def _notify_server_error(self, message: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item["error"] = {"message": message}
            item["event"].set()
        for thread_id in list(self._active_threads):
            event = {"type": "server.error", "threadId": thread_id, "error": message}
            if thread_id in self._active_job_ids:
                event["_job_id"] = self._active_job_ids[thread_id]
            self._on_event(event)
        self._active_threads.clear()
        self._active_job_ids.clear()
        with self._lifecycle_lock:
            self._initialized = False

    def start_turn(
        self,
        prompt: str,
        cwd: Path,
        requested_thread_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, str]:
        self._ensure_started()
        thread_id = requested_thread_id if requested_thread_id in self._threads else None
        if thread_id is None:
            result = self._request_raw(
                "thread/start",
                {
                    "cwd": str(cwd),
                    "ephemeral": True,
                    # Native UI has no approval dialog; match non-interactive `codex exec` behavior.
                    "approvalPolicy": "never",
                },
            )
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not thread_id:
                raise ValueError("Codex app-server 未返回 threadId")
            self._threads[str(thread_id)] = str(cwd)
        elif self._threads.get(thread_id) and Path(self._threads[thread_id]).resolve() != cwd.resolve():
            raise ValueError("会话工作目录不一致")
        self._active_threads[str(thread_id)] = True
        if job_id:
            self._active_job_ids[str(thread_id)] = job_id
        result = self._request_raw(
            "turn/start",
            {"threadId": str(thread_id), "input": [{"type": "text", "text": prompt}]},
        )
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not turn_id:
            raise ValueError("Codex app-server 未返回 turnId")
        return {"thread_id": str(thread_id), "turn_id": str(turn_id)}

    def interrupt(self, thread_id: Optional[str], turn_id: Optional[str]) -> None:
        if not thread_id or not turn_id:
            return
        try:
            self._request_raw("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except ValueError:
            return

    def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        self._initialized = False
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        self._threads.clear()
        self._active_threads.clear()
        self._active_job_ids.clear()

    def restart(self) -> None:
        with self._lifecycle_lock:
            self._terminate_locked()

    def warmup(self) -> None:
        self._ensure_started()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._terminate_locked()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "••••••"
    return value[:5] + "••••••" + value[-4:]


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".codex-manager-", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def parse_simple_toml(text: str) -> Dict[str, Dict[str, str]]:
    """Parse the scalar TOML fields used by Codex without rewriting unknown sections."""
    result: Dict[str, Dict[str, str]] = {"": {}}
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        section_match = re.match(r"^\[([^\]]+)\]$", line)
        if section_match:
            section = section_match.group(1)
            result.setdefault(section, {})
            continue
        value_match = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*$", line)
        if not value_match:
            continue
        key, value = value_match.groups()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        result.setdefault(section, {})[key] = value
    return result


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def replace_top_level(text: str, values: Dict[str, str]) -> str:
    lines = text.splitlines()
    positions: Dict[str, int] = {}
    first_section = len(lines)
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            first_section = min(first_section, index)
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s*=", line)
        if match:
            positions[match.group(1)] = index
    inserts: List[str] = []
    for key, value in values.items():
        rendered = "%s = %s" % (key, toml_string(value))
        if key in positions:
            lines[positions[key]] = rendered
        else:
            inserts.append(rendered)
    if inserts:
        lines[first_section:first_section] = inserts + ([""] if first_section else [])
    return "\n".join(lines).rstrip() + "\n"


def replace_section(text: str, section_name: str, content: str) -> str:
    lines = text.splitlines()
    header = "[%s]" % section_name
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        base = text.rstrip()
        return base + "\n\n" + header + "\n" + content.rstrip() + "\n"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("[") and lines[i].strip().endswith("]"):
            end = i
            break
    lines[start:end] = [header] + content.rstrip().splitlines()
    return "\n".join(lines).rstrip() + "\n"


class SecretStore:
    def __init__(self, root: Path):
        self.root = root
        self.file = root / "secrets.json"
        self._memory: Dict[str, str] = {}

    def _use_keychain(self) -> bool:
        return os.name == "posix" and os.uname().sysname == "Darwin" and os.getenv("CODEX_MANAGER_SECRET_MODE") != "file"

    def get(self, provider_id: str) -> Optional[str]:
        if self._use_keychain():
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "CodexManager", "-a", "provider:" + provider_id, "-w"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return result.stdout.strip() or None
            except (OSError, subprocess.CalledProcessError):
                return None
        if not self._memory and self.file.exists():
            self._memory = json.loads(self.file.read_text(encoding="utf-8"))
        return self._memory.get(provider_id)

    def set(self, provider_id: str, secret: str) -> None:
        if self._use_keychain():
            subprocess.run(
                ["security", "add-generic-password", "-U", "-s", "CodexManager", "-a", "provider:" + provider_id, "-w", secret],
                check=True,
                capture_output=True,
                text=True,
            )
            return
        self._memory[provider_id] = secret
        atomic_write(self.file, json.dumps(self._memory, ensure_ascii=False, indent=2) + "\n")

    def delete(self, provider_id: str) -> None:
        if self._use_keychain():
            subprocess.run(
                ["security", "delete-generic-password", "-s", "CodexManager", "-a", "provider:" + provider_id],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        self._memory.pop(provider_id, None)
        atomic_write(self.file, json.dumps(self._memory, ensure_ascii=False, indent=2) + "\n")


class ManagerService:
    def __init__(
        self,
        home: Optional[Path] = None,
        data_root: Optional[Path] = None,
        prewarm_app_server: bool = True,
    ):
        self.home = Path(home or Path.home())
        self.codex_root = self.home / ".codex"
        self.data_root = Path(data_root or (self.home / "Library" / "Application Support" / "CodexManager"))
        self.providers_file = self.data_root / "providers.json"
        self.backups_root = self.data_root / "backups"
        self.secret_store = SecretStore(self.data_root)
        self._lock = RLock()
        self._chat_jobs: Dict[str, Dict[str, Any]] = {}
        self._chat_jobs_lock = RLock()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._providers = self._load_providers()
        self.workspace = self._default_workspace()
        self._app_server = CodexAppServer(self._handle_app_server_event)
        atexit.register(self._app_server.close)
        if prewarm_app_server:
            threading.Thread(target=self._warm_app_server, daemon=True).start()

    @staticmethod
    def _default_workspace() -> Path:
        configured = os.getenv("CODEX_MANAGER_WORKSPACE")
        candidates = [
            Path(configured).expanduser() if configured else None,
            Path.home() / "Downloads" / "Codex`s bro",
            Path.cwd(),
        ]
        for candidate in candidates:
            if candidate and candidate.is_dir():
                return candidate.resolve()
        return Path.home().resolve()

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return copy.deepcopy(default)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return copy.deepcopy(default)

    def _load_providers(self) -> List[Dict[str, Any]]:
        saved = self._load_json(self.providers_file, {})
        providers = saved.get("providers") if isinstance(saved, dict) else None
        if isinstance(providers, list):
            return providers
        discovered = self._discover_providers()
        self._persist_providers(discovered)
        return discovered

    def _discover_providers(self) -> List[Dict[str, Any]]:
        config_path = self.codex_root / "config.toml"
        parsed = parse_simple_toml(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        models = self._load_json(self.codex_root / "models.json", {"models": []}).get("models", [])
        catalog = [self._model_meta(item) for item in models if isinstance(item, dict) and item.get("slug")]
        providers: List[Dict[str, Any]] = []
        for section, values in parsed.items():
            if not section.startswith("model_providers."):
                continue
            provider_id = section.split(".", 1)[1]
            if not PROVIDER_ID.match(provider_id):
                continue
            provider = {
                "id": provider_id,
                "name": values.get("name", provider_id),
                "base_url": values.get("base_url", ""),
                "wire_api": values.get("wire_api", "responses"),
                "default_model": parsed.get("", {}).get("model", ""),
                "enabled_models": [m["slug"] for m in catalog],
                "model_catalog": catalog,
                "last_check": None,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            token = values.get("experimental_bearer_token")
            if token:
                self.secret_store.set(provider_id, token)
            providers.append(provider)
        return providers

    def _persist_providers(self, providers: Optional[List[Dict[str, Any]]] = None) -> None:
        payload = {
            "version": 1,
            "providers": providers if providers is not None else self._providers,
        }
        atomic_write(self.providers_file, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _model_meta(item: Dict[str, Any]) -> Dict[str, Any]:
        fields = (
            "slug", "display_name", "description", "default_reasoning_level",
            "supported_reasoning_levels", "context_window", "max_context_window",
            "supports_reasoning_summaries", "supports_parallel_tool_calls",
            "supported_in_api", "visibility", "priority",
        )
        return {key: item[key] for key in fields if key in item}

    def _find(self, provider_id: str) -> Dict[str, Any]:
        for provider in self._providers:
            if provider.get("id") == provider_id:
                return provider
        raise ValueError("Provider 不存在")

    def _public_provider(self, provider: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(provider)
        secret = self.secret_store.get(provider["id"])
        result["api_key"] = {"configured": bool(secret), "masked": mask_secret(secret)}
        return result

    def status(self) -> Dict[str, Any]:
        config_path = self.codex_root / "config.toml"
        parsed = parse_simple_toml(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        top = parsed.get("", {})
        provider_id = top.get("model_provider", "")
        provider = next((p for p in self._providers if p.get("id") == provider_id), None)
        return {
            "model": top.get("model", ""),
            "provider_id": provider_id,
            "provider_name": provider.get("name", provider_id) if provider else provider_id,
            "config_path": str(config_path),
            "config_exists": config_path.exists(),
            "has_backup": bool(list(self.backups_root.glob("*/manifest.json"))) if self.backups_root.exists() else False,
        }

    def bootstrap(self) -> Dict[str, Any]:
        return {
            "status": self.status(),
            "providers": [self._public_provider(p) for p in self._providers],
            "workspace": str(self.workspace),
        }

    def save_provider(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            provider_id = str(payload.get("id", "")).strip()
            if not PROVIDER_ID.match(provider_id):
                raise ValueError("Provider ID 只能包含字母、数字、下划线和短横线")
            name = str(payload.get("name", provider_id)).strip() or provider_id
            base_url = str(payload.get("base_url", "")).strip().rstrip("/")
            wire_api = str(payload.get("wire_api", "responses"))
            if not base_url.startswith(("https://", "http://")):
                raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
            if wire_api not in WIRE_APIS:
                raise ValueError("不支持的接口类型")
            catalog = payload.get("model_catalog", [])
            if not isinstance(catalog, list):
                raise ValueError("模型目录格式无效")
            enabled = [str(item) for item in payload.get("enabled_models", [])]
            default_model = str(payload.get("default_model", "")).strip()
            existing = next((p for p in self._providers if p.get("id") == provider_id), None)
            provider = existing or {"id": provider_id, "created_at": now_iso()}
            provider.update({
                "name": name,
                "base_url": base_url,
                "wire_api": wire_api,
                "default_model": default_model,
                "enabled_models": enabled,
                "model_catalog": catalog,
                "updated_at": now_iso(),
            })
            if existing is None:
                self._providers.append(provider)
            self._persist_providers()
            return self._public_provider(provider)

    def set_secret(self, provider_id: str, api_key: str) -> Dict[str, Any]:
        with self._lock:
            provider = self._find(provider_id)
            value = str(api_key).strip()
            if not value:
                raise ValueError("API Key 不能为空")
            self.secret_store.set(provider_id, value)
            provider["updated_at"] = now_iso()
            self._persist_providers()
            return self._public_provider(provider)

    def delete_provider(self, provider_id: str) -> Dict[str, Any]:
        with self._lock:
            if self.status().get("provider_id") == provider_id:
                raise ValueError("当前正在使用的 Provider 不能删除")
            self._find(provider_id)
            self._providers = [p for p in self._providers if p.get("id") != provider_id]
            self.secret_store.delete(provider_id)
            self._persist_providers()
            return self.bootstrap()

    def _request_models(self, provider: Dict[str, Any]) -> List[Dict[str, Any]]:
        secret = self.secret_store.get(provider["id"])
        if not secret:
            raise ValueError("API Key 未配置")
        request = urllib.request.Request(
            provider["base_url"].rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + secret, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ValueError("API Key 无效或无权访问")
            raise ValueError("Provider 返回 HTTP %s" % exc.code)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError("Provider 请求失败：%s" % getattr(exc, "reason", "网络超时"))
        raw_models = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
        models: List[Dict[str, Any]] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            slug = item.get("id", item.get("slug"))
            if not slug:
                continue
            meta = self._model_meta(item)
            meta["slug"] = slug
            meta.setdefault("display_name", slug)
            models.append(meta)
        return models

    def fetch_models(self, provider_id: str) -> Dict[str, Any]:
        with self._lock:
            provider = self._find(provider_id)
            fetched = self._request_models(provider)
            current = {item.get("slug"): item for item in provider.get("model_catalog", []) if item.get("slug")}
            added: List[str] = []
            for item in fetched:
                slug = item.get("slug") or item.get("id")
                item["slug"] = slug
                if slug not in current:
                    current[slug] = item
                    added.append(slug)
            provider["model_catalog"] = list(current.values())
            provider["updated_at"] = now_iso()
            self._persist_providers()
            return {"provider": self._public_provider(provider), "added": added, "count": len(fetched)}

    def validate_provider(self, provider_id: str) -> Dict[str, Any]:
        with self._lock:
            provider = self._find(provider_id)
            try:
                self._request_models(provider)
                check = {"ok": True, "at": now_iso(), "error": None}
            except ValueError as exc:
                check = {"ok": False, "at": now_iso(), "error": str(exc)}
            provider["last_check"] = check
            self._persist_providers()
            return {"provider": self._public_provider(provider), "check": check}

    def _validate_for_activation(self, provider: Dict[str, Any]) -> None:
        enabled = provider.get("enabled_models", [])
        catalog_slugs = {item.get("slug") for item in provider.get("model_catalog", []) if item.get("slug")}
        if not provider.get("default_model"):
            raise ValueError("请先设置默认模型")
        if not enabled:
            raise ValueError("至少需要勾选一个模型")
        if provider["default_model"] not in enabled:
            raise ValueError("默认模型必须在勾选列表中")
        missing = [slug for slug in enabled if slug not in catalog_slugs]
        if missing:
            raise ValueError("模型目录中不存在：" + ", ".join(missing))
        if not self.secret_store.get(provider["id"]):
            raise ValueError("API Key 未配置")

    def _backup(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        directory = self.backups_root / stamp
        directory.mkdir(parents=True, exist_ok=False)
        files = {
            "config.toml": self.codex_root / "config.toml",
            "auth.json": self.codex_root / "auth.json",
            "models.json": self.codex_root / "models.json",
        }
        manifest: Dict[str, Any] = {"created_at": now_iso(), "files": {}}
        for name, path in files.items():
            if path.exists():
                target = directory / name
                shutil.copy2(path, target)
                manifest["files"][name] = {
                    "exists": True,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            else:
                manifest["files"][name] = {"exists": False}
        atomic_write(directory / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        return directory

    def _restore(self, directory: Path) -> None:
        for name in ("config.toml", "auth.json", "models.json"):
            source = directory / name
            target = self.codex_root / name
            if source.exists():
                atomic_write(target, source.read_text(encoding="utf-8"))
            elif target.exists():
                target.unlink()

    def _render_files(self, provider: Dict[str, Any]) -> Dict[str, str]:
        config_path = self.codex_root / "config.toml"
        original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        original = replace_top_level(original, {"model": provider["default_model"], "model_provider": provider["id"]})
        secret = self.secret_store.get(provider["id"])
        section = "\n".join([
            "name = %s" % toml_string(provider["name"]),
            "base_url = %s" % toml_string(provider["base_url"]),
            "wire_api = %s" % toml_string(provider["wire_api"]),
            "experimental_bearer_token = %s" % toml_string(secret or ""),
        ])
        config = replace_section(original, "model_providers." + provider["id"], section)
        models = {item.get("slug"): item for item in provider.get("model_catalog", []) if item.get("slug")}
        selected = [models[slug] for slug in provider.get("enabled_models", []) if slug in models]
        return {
            "config.toml": config,
            "auth.json": json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": secret}, ensure_ascii=False, indent=2) + "\n",
            "models.json": json.dumps({"models": selected}, ensure_ascii=False, indent=2) + "\n",
        }

    def activate_provider(self, provider_id: str, default_model: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            provider = self._find(provider_id)
            if default_model is not None:
                provider["default_model"] = default_model
            self._validate_for_activation(provider)
            backup = self._backup()
            try:
                rendered = self._render_files(provider)
                self.codex_root.mkdir(parents=True, exist_ok=True)
                for name, content in rendered.items():
                    atomic_write(self.codex_root / name, content)
                parsed = parse_simple_toml((self.codex_root / "config.toml").read_text(encoding="utf-8"))
                if parsed.get("", {}).get("model_provider") != provider_id:
                    raise ValueError("写入后的 config.toml 校验失败")
                result = self._request_models(provider)
                check = {"ok": True, "at": now_iso(), "error": None, "model_count": len(result)}
                provider["last_check"] = check
                provider["updated_at"] = now_iso()
                self._persist_providers()
                self._restart_app_server()
                return {"status": "succeeded", "provider": self._public_provider(provider), "check": check}
            except Exception as exc:
                self._restore(backup)
                provider["last_check"] = {"ok": False, "at": now_iso(), "error": str(exc)}
                self._persist_providers()
                return {"status": "rolled_back", "error": str(exc), "provider": self._public_provider(provider)}

    def rollback(self) -> Dict[str, Any]:
        with self._lock:
            backups = sorted(self.backups_root.glob("*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
            if not backups:
                raise ValueError("没有可用的回滚备份")
            directory = backups[0].parent
            self._restore(directory)
            self._restart_app_server()
            return {"status": "succeeded", "restored_from": str(directory), "status_snapshot": self.status()}

    def open_config(self) -> Dict[str, Any]:
        path = self.codex_root / "config.toml"
        if not path.exists():
            raise ValueError("config.toml 不存在")
        if os.name == "posix" and os.uname().sysname == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            raise ValueError("当前平台暂不支持打开配置文件")
        return {"path": str(path)}

    def _handle_app_server_event(self, event: Dict[str, Any]) -> None:
        thread_id = event.get("threadId") or event.get("thread_id")
        job_id = event.get("_job_id")
        with self._chat_jobs_lock:
            job = self._chat_jobs.get(str(job_id)) if job_id else None
            if not job:
                job = next(
                    (item for item in self._chat_jobs.values() if item.get("thread_id") == thread_id and not item["done"]),
                    None,
                )
            if not job:
                return
            job["events"].append(event)
            event_type = event.get("type")
            if event_type == "agent_message_delta":
                job["answer"] += str(event.get("delta") or "")
            elif event_type == "item.completed":
                text = codex_event_text(event)
                if text and not job["answer"]:
                    job["answer"] = text
            elif event_type == "turn.completed":
                turn = event.get("turn") if isinstance(event.get("turn"), dict) else {}
                error = turn.get("error")
                if error:
                    job["error"] = self._format_turn_error(error)
                if job.get("stop_requested"):
                    job["error"] = "已停止"
                job["returncode"] = 0 if not job.get("error") else 1
                job["done"] = True
            elif event_type == "server.error":
                job["error"] = str(event.get("error") or "Codex app-server 已断开")
                job["returncode"] = 1
                job["done"] = True

    @staticmethod
    def _format_turn_error(error: Any) -> str:
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            for key in ("message", "codexErrorInfo", "type"):
                value = error.get(key)
                if value:
                    return str(value)
        return "Codex 执行失败"

    def _restart_app_server(self) -> None:
        with self._chat_jobs_lock:
            for job in self._chat_jobs.values():
                if not job["done"]:
                    job["error"] = "Codex 配置已更新，请重新发送任务"
                    job["returncode"] = 1
                    job["done"] = True
        self._app_server.restart()

    def _warm_app_server(self) -> None:
        try:
            self._app_server.warmup()
        except (OSError, ValueError):
            # A send attempt will retry and surface a user-facing error if startup still fails.
            return

    def start_chat(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("消息不能为空")
        workdir = Path(cwd).expanduser().resolve() if cwd else self.workspace
        if not workdir.is_dir():
            raise ValueError("工作目录不存在：%s" % workdir)
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "events": [],
            "answer": "",
            "error": None,
            "returncode": None,
            "thread_id": None,
            "turn_id": None,
            "done": False,
            "stop_requested": False,
        }
        with self._chat_jobs_lock:
            self._chat_jobs[job_id] = job
        try:
            turn = self._app_server.start_turn(prompt, workdir, thread_id, job_id)
            with self._chat_jobs_lock:
                job["thread_id"] = turn["thread_id"]
                job["turn_id"] = turn["turn_id"]
        except (OSError, ValueError) as exc:
            with self._chat_jobs_lock:
                job["error"] = str(exc)
                job["returncode"] = 1
                job["done"] = True
            raise ValueError("无法启动 Codex：%s" % exc)
        return {"job_id": job_id, "thread_id": turn["thread_id"], "turn_id": turn["turn_id"]}

    def poll_chat(self, job_id: str) -> Dict[str, Any]:
        with self._chat_jobs_lock:
            job = self._chat_jobs.get(str(job_id))
            if not job:
                raise ValueError("聊天任务不存在或已过期")
            return {
                "job_id": str(job_id),
                "done": job["done"],
                "events": copy.deepcopy(job["events"]),
                "answer": job["answer"],
                "error": job["error"],
                "thread_id": job["thread_id"],
                "turn_id": job["turn_id"],
                "returncode": job["returncode"],
            }

    def stop_chat(self, job_id: str) -> Dict[str, Any]:
        with self._chat_jobs_lock:
            job = self._chat_jobs.get(str(job_id))
            if not job:
                raise ValueError("聊天任务不存在或已过期")
            if job["done"]:
                return {"stopped": False, "done": True}
            job["stop_requested"] = True
            thread_id = job.get("thread_id")
            turn_id = job.get("turn_id")
        self._app_server.interrupt(thread_id, turn_id)
        return {"stopped": True, "done": False}


class Bridge:
    """Small pywebview-facing adapter. Methods return JSON-compatible values."""

    def __init__(self, service: ManagerService):
        self.service = service

    def bootstrap(self) -> Dict[str, Any]:
        return self.service.bootstrap()

    def save_provider(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.service.save_provider(payload)

    def set_secret(self, provider_id: str, api_key: str) -> Dict[str, Any]:
        return self.service.set_secret(provider_id, api_key)

    def delete_provider(self, provider_id: str) -> Dict[str, Any]:
        return self.service.delete_provider(provider_id)

    def fetch_models(self, provider_id: str) -> Dict[str, Any]:
        return self.service.fetch_models(provider_id)

    def validate_provider(self, provider_id: str) -> Dict[str, Any]:
        return self.service.validate_provider(provider_id)

    def activate_provider(self, provider_id: str, default_model: Optional[str] = None) -> Dict[str, Any]:
        return self.service.activate_provider(provider_id, default_model)

    def rollback(self) -> Dict[str, Any]:
        return self.service.rollback()

    def open_config(self) -> Dict[str, Any]:
        return self.service.open_config()

    def start_chat(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.service.start_chat(prompt, cwd, thread_id)

    def poll_chat(self, job_id: str) -> Dict[str, Any]:
        return self.service.poll_chat(job_id)

    def stop_chat(self, job_id: str) -> Dict[str, Any]:
        return self.service.stop_chat(job_id)
