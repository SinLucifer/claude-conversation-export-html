# Releasing

本文件面向维护者，记录发布到 PyPI 的流程。

## 前置条件

1. 在 PyPI 创建项目：`claude-conversation-export-html`
2. 在仓库 GitHub Actions Secrets 设置：
   - `PYPI_API_TOKEN`
3. 仓库已包含发布工作流：`.github/workflows/publish.yml`

## 发布步骤

1. 更新版本号（`pyproject.toml` -> `project.version`）
2. 提交变更并推送主分支
3. 打 tag 并推送

```bash
git tag v0.1.1
git push origin main
git push origin v0.1.1
```

4. 在 GitHub Actions 查看 `Publish to PyPI` 工作流结果

