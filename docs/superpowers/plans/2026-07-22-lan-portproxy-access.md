# LAN PortProxy Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许局域网 `192.168.1.0/24` 通过 `http://192.168.1.108:8090` 访问仍监听于 `127.0.0.1:8090` 的闲鱼管理后台。

**Architecture:** 不修改应用和绿色版管理器。使用 Windows TCP PortProxy 将局域网地址转发至回环地址，并用 Windows 防火墙将来源限制在当前 `/24` 局域网；未来穿透仍直接连接回环地址。

**Tech Stack:** Windows PowerShell、netsh interface portproxy、Windows Defender Firewall、HTTP 健康检查。

---

### Task 1: 保存变更前状态

**Files:**
- Create: `C:\business\xianyu-auto-reply-portable\backups\lan-access-<timestamp>\portproxy-before.txt`
- Create: `C:\business\xianyu-auto-reply-portable\backups\lan-access-<timestamp>\firewall-before.txt`
- Create: `C:\business\xianyu-auto-reply-portable\backups\lan-access-<timestamp>\network-before.txt`

- [ ] **Step 1:** 导出 `netsh interface portproxy show all`。
- [ ] **Step 2:** 导出名称包含 `XianyuAutoReply-LAN-8090` 的防火墙规则。
- [ ] **Step 3:** 记录当前 IPv4、网络类型、8090 监听和 PID。

### Task 2: 建立局域网 TCP 转发

- [ ] **Step 1:** 确认 `iphlpsvc` 存在并启动。
- [ ] **Step 2:** 删除完全相同的旧 PortProxy 规则，保证操作幂等。
- [ ] **Step 3:** 添加 `192.168.1.108:8090 -> 127.0.0.1:8090` 的 IPv4-to-IPv4 规则。
- [ ] **Step 4:** 重新读取规则并核对四个地址/端口字段。

### Task 3: 建立最小范围防火墙规则

- [ ] **Step 1:** 删除同名旧规则，保证操作幂等。
- [ ] **Step 2:** 创建 TCP 入站允许规则：本地地址 `192.168.1.108`、本地端口 `8090`、远程地址 `192.168.1.0/24`、配置文件 `Any`。
- [ ] **Step 3:** 读取规则及端口筛选器、地址筛选器，确认没有 `RemoteAddress=Any`。

### Task 4: 验证访问和业务连续性

- [ ] **Step 1:** 请求 `http://127.0.0.1:8090/health`，预期 HTTP 200。
- [ ] **Step 2:** 请求 `http://192.168.1.108:8090/health`，预期 HTTP 200。
- [ ] **Step 3:** 请求 `http://192.168.1.108:8090/login.html`，预期 HTTP 200 且返回登录页面。
- [ ] **Step 4:** 确认原 Python PID 仍运行且回环监听未被替换。
- [ ] **Step 5:** 检查最新日志，确认 WebSocket 心跳继续正常。
- [ ] **Step 6:** 提示用户从另一台局域网设备完成最终浏览器验证。

### Task 5: 准备回滚信息

- [ ] **Step 1:** 记录删除 PortProxy 的精确命令。
- [ ] **Step 2:** 记录删除防火墙规则的精确命令。
- [ ] **Step 3:** 将最终规则和验证结果写入备份目录的 `result.txt`。
