# claude-conversation-export-html

将 Claude Code 的 `.jsonl` 会话导出为**自包含 HTML**。

命令名：`claude-html-start`

## 安装

```bash
pipx install claude-conversation-export-html
claude-html-start --help
```

## 快速开始

默认读取 `~/.claude/projects`：

```bash
claude-html-start
```

非交互导出单个会话：

```bash
claude-html-start -i /path/to/session.jsonl --non-interactive -o session.html
```

非交互导出目录中指定会话：

```bash
claude-html-start -i ~/.claude/projects --non-interactive -s 1,3-5 -o export.html
```

导出全部会话：

```bash
claude-html-start -i ~/.claude/projects --non-interactive --all -o export.html
```

## 参数

- `-i, --input`：输入文件或目录（默认 `~/.claude/projects`）
- `-o, --output`：输出 HTML 路径
- `-s, --select`：会话编号选择，如 `1,3-5`
- `--all`：导出全部会话
- `--title`：HTML 标题
- `--non-interactive`：关闭交互（目录输入需配合 `--all` 或 `--select`）

## 交互快捷键

- `↑/↓` 或 `j/k`：移动
- `n / p`：翻页
- `Enter` / `Space`：勾选
- `/`：筛选
- `a`：勾选当前页
- `c`：清空
- `e`：导出
- `q`：退出

## 发布（GitHub + PyPI）

仓库已包含：
- `pyproject.toml`
- `.github/workflows/ci.yml`
- `.github/workflows/publish.yml`

发布步骤：
1. 在 PyPI 创建项目 `claude-conversation-export-html`
2. 在 GitHub Actions Secrets 设置 `PYPI_API_TOKEN`
3. 更新 `pyproject.toml` 的 `version`
4. 打 tag 并推送：

```bash
git tag v0.1.1
git push origin main
git push origin v0.1.1
```

