# CodexManager

CodexManager 是一个面向 macOS 的 Codex 桌面管理器。它把 Provider 与模型配置管理、Codex 对话执行和本地工作区访问整合到一个原生窗口中，让用户不需要手动编辑 `~/.codex` 配置文件，也不需要单独启动本地 Web 服务。

## 项目简介

CodexManager 通过 pywebview 提供桌面窗口，通过 Python JS Bridge 连接本地 Codex CLI。应用会复用一个持久化的 `codex app-server --stdio` 连接，减少每条消息重复启动进程带来的等待时间，并在同一会话中保留 Codex 上下文。

它适合需要频繁切换模型、Provider 和项目目录的 Codex 用户，也适合作为一个轻量的本地 Codex 前端进行代码编写、文件修改、命令执行和结果查看。

## 核心功能

- Provider 管理：新增、编辑、删除和查看多个 Provider。
- 模型管理：读取、编辑、启用和验证模型目录。
- 一键切换：安全更新 `config.toml`、`auth.json` 和 `models.json`。
- 自动备份与回滚：配置切换失败时自动恢复，也可以手动回滚上一份配置。
- 持久化聊天：复用 Codex app-server，连续消息共享同一会话上下文。
- 实时输出：显示 Codex 回复增量、命令执行过程和文件修改步骤。
- 工作区管理：选择主工作目录，额外添加项目目录、桌面或其他授权目录。
- 内容复制：回复、命令输出和 diff 结果均可直接复制。
- 本地优先：前端运行在应用内嵌 WebView 中，不依赖公开 HTTP 端口。
- 密钥保护：macOS 优先使用 Keychain 保存 API Key，界面列表只显示掩码。

## 技术栈

- Python 3.9+
- pywebview
- Codex CLI app-server
- HTML / CSS / JavaScript
- PyInstaller

## 运行方式

确保本机已经安装并登录 Codex CLI，然后安装 Python 依赖：

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

直接打开 `main.html` 可以查看浏览器预览界面，但浏览器预览不会执行真实的 Codex 本地桥接，也不能打开原生目录授权窗口。

## 构建 macOS 应用

```bash
./build_app.sh
```

构建结果为：

```text
dist/CodexManager.app
```

`build/`、`dist/`、Python 缓存和本地配置不会提交到 Git，因为它们已被 `.gitignore` 排除。

## 工作区权限

CodexManager 不会默认授予整个 Mac 的访问权限。应用启动时使用当前项目目录作为主工作区；用户可以在顶部工作区菜单中：

1. 选择新的主工作目录；
2. 添加额外目录；
3. 直接添加桌面目录；
4. 移除不再需要的额外目录。

这些目录会作为 Codex thread 的 `runtimeWorkspaceRoots` 传入，在保留沙盒隔离的同时允许 Codex 访问用户明确选择的位置。macOS 仍可能要求在“系统设置 → 隐私与安全性 → 文件与文件夹”中授予 `CodexManager.app` 相应目录权限。

## 配置文件

应用读取和更新以下 Codex 文件：

```text
~/.codex/config.toml
~/.codex/auth.json
~/.codex/models.json
```

CodexManager 自身的 Provider、工作区和备份数据保存在：

```text
~/Library/Application Support/CodexManager/
```

## 测试

运行后端测试：

```bash
python3 -m unittest discover -s tests -v
```

当前测试覆盖配置解析、Provider 状态、模型切换、自动回滚、持久化聊天、工作区根目录和 Codex 事件处理。

## 项目状态

当前版本是一个可运行的 macOS MVP，重点覆盖 Provider / 模型管理、本地 Codex 对话和工作区权限控制。后续可以继续扩展会话持久化、附件输入、文件树、更多平台支持和发布包自动化。
