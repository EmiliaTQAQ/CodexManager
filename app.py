from __future__ import annotations

import os
import sys
from pathlib import Path

import webview

from backend.service import Bridge, ManagerService


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def main() -> None:
    service = ManagerService()
    api = Bridge(service)
    window = webview.create_window(
        "Codex 模型管理器",
        resource_path("main.html").as_uri() + "#native",
        js_api=api,
        width=1180,
        height=760,
        min_size=(800, 560),
        resizable=True,
    )
    api.set_window(window)

    def pick_folder(initial: str):
        selected = window.create_file_dialog(webview.FOLDER_DIALOG, directory=initial)
        return selected[0] if selected else None

    api.set_folder_picker(pick_folder)
    webview.start(debug=os.getenv("CODEX_MANAGER_DEBUG") == "1")


if __name__ == "__main__":
    main()
