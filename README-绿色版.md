# zhinianboke/xianyu-auto-reply 绿色版

本目录是 Windows 本地部署入口，保留 `runtime/`、`logs/`、`backups/`、`state/` 等环境目录。

## 依赖

- MySQL 8.0+
- Redis 6+
- Python / Node / Git 已随 `runtime/` 保留

## 常用命令

```bat
VERIFY.cmd
START.cmd
STATUS.cmd
UPDATE.cmd --check-only
STOP.cmd
```

## 端口

- `backend-web` - `8089`
- `websocket` - `8090`
- `scheduler` - `8091`
- `frontend` - `9000`

## 说明

- 默认不启动 `promotion/`
- 更新任务名：`XianyuAutoReply-Zhinianboke-Portable-Update`
- 计划任务默认每天 `03:30` 执行
