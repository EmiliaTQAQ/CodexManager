from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional


WIRE_APIS = {"responses", "chat"}
PROVIDER_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


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
    def __init__(self, home: Optional[Path] = None, data_root: Optional[Path] = None):
        self.home = Path(home or Path.home())
        self.codex_root = self.home / ".codex"
        self.data_root = Path(data_root or (self.home / "Library" / "Application Support" / "CodexManager"))
        self.providers_file = self.data_root / "providers.json"
        self.backups_root = self.data_root / "backups"
        self.secret_store = SecretStore(self.data_root)
        self._lock = RLock()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._providers = self._load_providers()

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
        return {"status": self.status(), "providers": [self._public_provider(p) for p in self._providers]}

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
