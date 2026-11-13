# CodeX Project Finder · CodeX 项目查找器

[中文](#中文) | [English](#english)

## 中文

CodeX 项目查找器是一款 Windows 本地工具，帮你找回保存在本机的 Codex 对话。项目多、聊天多时，有些较早的对话可能不容易在 Codex 原界面里找到：可以在这里按项目、标题或对话正文搜索，再打开原对话继续工作。

全文搜索支持短关键词，包括单个字符或字母。它查找的是已索引的本地对话内容，而不是推测未同步的记录；输入越短，结果通常越多。最近对话视图只搜索标题，查正文请切换到全文搜索。

### 功能

- 查看最近对话、项目和最后已知聊天时间，搜索、筛选和收藏。
- 搜索本地对话中的用户和助手文字；支持中文、字母、数字和短词组合。
- 在 Codex 桌面端打开原对话；也可双击对话，在 Windows Terminal 中用 Codex CLI 接续。
- 以只读方式读取本机 Codex 记录，创建独立搜索索引；不会发送消息或自动执行对话里的命令。
- 仅在本机回环地址提供界面，并为本地 API 设置随机会话令牌。

### 使用

在 [Releases](https://github.com/jkdodin2003/codex-session-finder/releases/latest) 下载 Windows x64 ZIP，把整个文件夹解压到可写位置，双击 `CodeXProjectFinder.exe`。无需另装 Python，不要单独移动 EXE。程序需要本机 Codex 记录才有内容可搜；CLI 接续还需要 Windows Terminal 和 Codex CLI。

从源码启动需要 Python 3.10 或更新版本，以及支持 FTS5 trigram 分词器的 SQLite。在项目目录打开 PowerShell，运行：

```powershell
python -X utf8 app.py --open
```

首次建立索引可能需要一些时间。选择“全文搜索”输入关键词、按 Enter，点击结果预览；双击或使用界面按钮可尝试打开原对话。原项目路径不可用、会话被其他程序占用或本机缺少 CLI 时，需要先处理对应问题。

### 数据与隐私

工具默认读取当前用户的 `~/.codex`，支持 `CODEX_HOME` 环境变量。索引、收藏、日志和运行时令牌保存在本工具的 `outputs/`，可能包含敏感对话，**不要上传或分享这个目录**。Git 忽略规则不等于安全保证，分享代码前仍请检查提交内容。

只处理本机可读取的记录；云端或其他机器未同步的对话不会凭空出现。界面会对部分常见密钥格式做显示脱敏，但不保证覆盖所有敏感信息。搜索结果不等于完整备份。Codex 数据库格式和启动方式未来变化时可能需要适配。

### 测试

```powershell
python -X utf8 selfcheck.py
```

测试使用临时合成数据，不写入原聊天数据库。欢迎使用、反馈问题和提出改进建议。

## English

CodeX Project Finder is a local Windows tool for finding conversations stored by Codex on your computer. When older conversations become difficult to locate in the Codex interface, search by project, title, or conversation text and open the original session to continue.

Full-text search supports short queries, including a single character or letter. Results are limited to local content that has been indexed; short queries can return many matches. The Recent view searches titles only—switch to full-text search to search message bodies.

### Features

- Browse recent conversations, projects, and the last known chat time; search, filter, and bookmark.
- Search indexed user and assistant text in Chinese and other languages.
- Open an existing conversation in Codex Desktop or resume it in Codex CLI through Windows Terminal.
- Read the original Codex data in read-only mode and maintain a separate local index. The tool does not send messages or run commands from conversation history.
- Serve the UI only on loopback with a random session token for the local API.

### Getting started

Download the Windows x64 ZIP from [Releases](https://github.com/jkdodin2003/codex-session-finder/releases/latest), extract the entire folder to a writable location, and double-click `CodeXProjectFinder.exe`. Python is bundled; keep the EXE with the extracted files. You need local Codex records to search, and Windows Terminal and Codex CLI to resume a session in the CLI.

To run from source, use Python 3.10 or newer with SQLite FTS5 trigram support. From the project directory, run:

```powershell
python -X utf8 app.py --open
```

Initial indexing may take time. Select full-text search for message contents. Missing project paths, conversations open elsewhere, or unavailable CLI installations may prevent resuming a session.

### Privacy

The tool reads `~/.codex` by default and supports `CODEX_HOME`. Its own index, bookmarks, logs, and runtime token are stored in `outputs/` and may contain private conversation data. **Never publish or share that directory.** The ignore file is not a substitute for reviewing commits.

Only locally available records can be searched. Common key patterns are masked in the UI on a best-effort basis, not as a comprehensive secret scanner. The index is not a full backup. Future Codex data format changes may require updates.

Run `python -X utf8 selfcheck.py` for isolated checks using synthetic data. Contributions and feedback are welcome.

## License

MIT. See [LICENSE](LICENSE). This independent project is not affiliated with or endorsed by OpenAI.
