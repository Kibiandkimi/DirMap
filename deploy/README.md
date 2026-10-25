# GitHub 自动部署

向 `main` 推送后，[工作流](../.github/workflows/deploy.yml) 在 Ubuntu 24.04 / Python 3.12 / Node 24 中运行后端、API、任务恢复、部署和前端测试，构建网页，再上传经过哈希校验的发布包。PR 只测试，不能部署。也可在 Actions 页面手动运行最新 `main`。

部署沿用 `DirMapDeploy` 的服务器结构：`/opt/dirmap/releases/` 保存版本，`current` 原子切换，systemd 重启应用，`data/` 继续保存教学数据。发布包不含上传地图、校核状态、任务、课堂包、教师口令、证书或 nginx 配置。

服务器用 `/api/health` 中的 `revision` 核实新进程确实运行了本次提交，再由 Actions 校验公网 HTTPS。启动或内部健康检查失败时恢复旧版本；公网不可达但内部正常时工作流报告失败并保留正常服务。成功后保留最近五个发布版本，其中包含当前版本和上一个版本。

Python 依赖与现有环境完全一致时复用它；锁文件变化则在 `venvs/<锁文件 SHA256>/` 创建独立环境，每个发布目录记录自己的 `.venv` 链接。回滚会同时恢复代码和依赖。旧手动发布版本没有 `.venv` 时，启动脚本仍使用原来的 `/opt/dirmap/venv`。历史依赖环境不自动删除。

并发发布在 GitHub 和服务器端串行执行；等待期间被较新 `main` 替代的提交跳过部署，避免慢测试把服务退回旧代码。进行中的部署不会因下一次推送被自动取消。

## 一次性配置

已有服务器需要免密 SSH、`sudo -n systemctl restart dirmap.service`、系统 Python/venv、现有 DirMap 目录和可信 HTTPS。首次安装 nginx、TLS、OCR、数据和基础环境仍可使用原 `~/Work/temp/DirMapDeploy` 工具。

1. 生成专用 Ed25519 SSH 密钥，把**公钥**及本目录的 `receiver.py`、`run-server.sh`、`install-receiver.sh` 复制到服务器临时目录。
2. 以现有部署用户执行 `bash install-receiver.sh github-actions.pub`。安装器添加受限公钥、接收器和兼容旧版本的 systemd 启动入口，重新加载 systemd 配置；应用在下一次部署时重启。公钥仅允许 `probe` 和经过校验的发布协议，不能打开 SSH 终端或转发端口。
3. 在 GitHub 创建 `production` Environment，只允许 `main` 部署，添加以下 Environment Secrets：

| Secret | 内容 |
| --- | --- |
| `DEPLOY_HOST` | 服务器 IP 或域名 |
| `DEPLOY_PORT` | SSH 端口 |
| `DEPLOY_USER` | 部署用户名 |
| `DEPLOY_SSH_KEY` | 专用部署私钥，含完整首尾行 |
| `DEPLOY_KNOWN_HOSTS` | 已通过现有可信连接核对的 SSH 主机公钥记录 |

4. 添加仓库 Variable `DEPLOY_URL`，例如 `https://example.com`。工作流严格校验 SSH 主机公钥和 HTTPS 证书，不自动信任新主机。
5. 推送并查看 Actions 的 `Test and deploy DirMap`。成功记录包括提交 SHA，公网 `/api/health` 应返回同一个 `revision`。

部署私钥、主机口令和 `config.env` 不提交到仓库。密钥轮换时更新 GitHub Secret 和服务器 `authorized_keys` 中对应的专用公钥；保留个人 SSH 登录密钥。

## 复现与恢复

本地检查：

```bash
source tools/runtime-env.sh
python3 -B -m pytest -q tests/test_deploy.py
bash -n deploy/*.sh
npm run build
python3 -B deploy/package.py --revision "$(git rev-parse HEAD)" --output /tmp/dirmap-release.tar.gz
```

包中的 `deployment.json` 记录提交及每个文件的哈希。接收器拒绝越界路径、符号链接、数据目录、重复文件和哈希不一致的包；不会把仓库文件覆盖到共享教学数据中。新提交需要的依赖安装失败时，旧服务继续运行。

自动回滚后，在 GitHub 修复失败提交再推送，或重新运行最新提交的工作流。原手动工具的 `deploy-azure.sh rollback` 仍可用于历史版本。不要在 Actions 正在发布时同时运行旧手动更新脚本。

自动化检查不等于真机传感器或校园路线验收；教学发布前仍须按应用中的核验流程确认。
