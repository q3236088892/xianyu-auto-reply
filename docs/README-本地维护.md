# 闲鱼自动回复绿色版：本地维护入口

本目录是 `GuDong2003/xianyu-auto-reply-fix` 的 Windows x64 源码绿色部署。正式入口均在根目录：`START.cmd`、`STOP.cmd`、`STATUS.cmd`、`VERIFY.cmd`、`UPDATE.cmd`、`ROLLBACK.cmd`、`INSTALL-TASK.cmd`、`REMOVE-TASK.cmd`。

当前生产地址：`http://127.0.0.1:8090`。默认不开放局域网或公网。

维护顺序：先 `STATUS.cmd`，变更前备份整个目录，变更后运行 `VERIFY.cmd`。日志位于 `logs/app` 与 `logs/maintenance`。
