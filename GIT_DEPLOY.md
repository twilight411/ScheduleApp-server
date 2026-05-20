# 后端 Git 部署

## 分支

- `main`：生产稳定分支

## 首次接入（已完成可跳过）

```bash
OLD_RUNTIME=/app/spirit-scheduler-pre-git-20260516-161024 REPO_URL=https://github.com/twilight411/ScheduleApp-server.git BRANCH=main bash scripts/bootstrap_git_on_server.sh
```

## 日常发布

```bash
cd /app/spirit-scheduler
bash scripts/deploy_backend.sh main
```

若服务器访问 GitHub 超时，在开发机执行：

```powershell
python .deploy/deploy_server_from_local_git.py
```

- `.env`、`spirit.db`、`web_build/` **不入库**，保留在服务器本地。
- 旧目录 `/app/spirit-scheduler-pre-git-20260516-161024` 仅作备份，systemd 已指向 `/app/spirit-scheduler`。
