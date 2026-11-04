"""本地 SOCKS5 认证中继。

Chromium 不支持带用户名密码的 SOCKS5 代理（Playwright launch 校验直接拒绝），
而 httpx/aiohttp 支持。对带认证的 SOCKS5 上游（如 127.0.0.1 上的 AdsPower 类
本机转发器），启动一个一次性本地中继：

    Chromium -> 127.0.0.1:中继端口（免密 socks5） -> 上游（socks5 + 账号密码） -> 目标

中继只监听 127.0.0.1 随机端口，生命周期绑定单次发布，用完即关。
域名以 ATYP=3 原样转发给上游解析（代理侧解析 DNS，与本项目 socks5 语义一致）。
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from typing import Optional

from loguru import logger


class Socks5AuthRelay:
    """把带认证的 SOCKS5 上游转换为本地免密 SOCKS5 端口。"""

    def __init__(self, upstream_host: str, upstream_port: int,
                 username: str, password: str):
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._username = username.encode("utf-8")
        self._password = password.encode("utf-8")
        self._server: Optional[asyncio.AbstractServer] = None
        self._client_tasks: set[asyncio.Task] = set()
        self.port: int = 0

    async def start(self) -> "Socks5AuthRelay":
        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", 0,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info(
            f"🔄 SOCKS5 认证中继已启动: 127.0.0.1:{self.port} -> "
            f"{self._upstream_host}:{self._upstream_port}（带认证）"
        )
        return self

    @property
    def proxy_dict(self) -> dict:
        """给 Chromium launch 用的免密 proxy dict。"""
        return {"server": f"socks5://127.0.0.1:{self.port}"}

    async def close(self) -> None:
        # 顺序很关键：Python 3.12+ Server.wait_closed() 会等待所有现存连接结束，
        # 必须先停接受新连接 → 取消所有客户端任务 → 最后才 wait_closed（且带超时），
        # 否则浏览器留下的悬挂连接会让 close() 卡死（生产事故实锤）。
        if self._server:
            self._server.close()
        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            # 各客户端 task 的 finally 会等待底层连接关闭，个别传输层句柄可能
            # 永不完成（浏览器已退出但代理侧连接悬挂），必须加超时防止整体卡死
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*self._client_tasks, return_exceptions=True),
                    timeout=5,
                )
        self._client_tasks.clear()
        if self._server:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=3)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._client_tasks.add(task)
        try:
            await self._relay_connection(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"SOCKS5 中继连接处理失败: {exc}")
        finally:
            self._client_tasks.discard(task)
            writer.close()
            # wait_closed 在 Proactor 事件循环下可能永不返回（对端悬挂），
            # 加超时兜底，绝不阻塞 close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=2)

    async def _relay_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # 1) 客户端问候：VER=5, NMETHODS, METHODS —— 只回免密
        head = await reader.readexactly(2)
        if head[0] != 5:
            raise ValueError(f"客户端 SOCKS 版本不支持: {head[0]}")
        await reader.readexactly(head[1])
        writer.write(b"\x05\x00")
        await writer.drain()

        # 2) 客户端请求：VER CMD RSV ATYP ADDR PORT
        req = await reader.readexactly(4)
        if req[1] != 1:
            writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")  # command not supported
            await writer.drain()
            return
        atyp = req[3]
        if atyp == 1:
            raw_addr = await reader.readexactly(4)
            addr = raw_addr
            target = ".".join(str(b) for b in raw_addr)
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            raw_addr = await reader.readexactly(ln)
            addr = bytes([ln]) + raw_addr
            target = raw_addr.decode("utf-8", "replace")
        elif atyp == 4:
            raw_addr = await reader.readexactly(16)
            addr = raw_addr
            target = str(ipaddress.IPv6Address(raw_addr))
        else:
            raise ValueError(f"未知 ATYP: {atyp}")
        port = int.from_bytes(await reader.readexactly(2), "big")
        target = f"{target}:{port}"

        # 3) 连上游并完成带认证问候
        up_reader, up_writer = await asyncio.open_connection(
            self._upstream_host, self._upstream_port,
        )
        try:
            try:
                up_writer.write(b"\x05\x02\x00\x02")  # 提供 免密/用户名密码 两种
                await up_writer.drain()
                resp = await up_reader.readexactly(2)
                if resp[0] != 5:
                    raise ValueError(f"上游 SOCKS 版本异常: {resp[0]}")
                method = resp[1]
                if method == 0x02:
                    up_writer.write(
                        b"\x01" + bytes([len(self._username)]) + self._username
                        + bytes([len(self._password)]) + self._password,
                    )
                    await up_writer.drain()
                    auth = await up_reader.readexactly(2)
                    if auth[1] != 0:
                        raise PermissionError(f"上游 SOCKS5 认证失败, status={auth[1]}")
                elif method != 0x00:
                    raise ValueError(f"上游不接受任何认证方式: {method}")

                # 4) 透传 connect 请求并回读结果
                up_writer.write(b"\x05\x01\x00" + bytes([atyp]) + addr + port.to_bytes(2, "big"))
                await up_writer.drain()
                up_resp = await up_reader.readexactly(4)
                rep = up_resp[1]
                up_atyp = up_resp[3]
                if up_atyp == 1:
                    await up_reader.readexactly(4 + 2)
                elif up_atyp == 3:
                    ln = (await up_reader.readexactly(1))[0]
                    await up_reader.readexactly(ln + 2)
                elif up_atyp == 4:
                    await up_reader.readexactly(16 + 2)
            except (PermissionError, ConnectionError, ValueError, asyncio.IncompleteReadError) as up_exc:
                # 认证失败/上游拒连：给客户端回通用失败 REP，再抛给上层记日志
                logger.warning(f"SOCKS5 中继目标 {target} 握手失败: {up_exc}")
                writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                raise
            writer.write(b"\x05" + bytes([rep]) + b"\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            if rep != 0:
                raise ConnectionError(f"上游代理连接目标 {target} 失败, REP={rep}")

            # 5) 双向桥接
            await self._pipe(reader, writer, up_reader, up_writer)
        finally:
            up_writer.close()
            # 同客户端侧：Proactor 下对端悬挂时 wait_closed 可能永不返回，加超时
            with contextlib.suppress(Exception):
                await asyncio.wait_for(up_writer.wait_closed(), timeout=2)

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    up_reader: asyncio.StreamReader, up_writer: asyncio.StreamWriter) -> None:
        async def forward(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                dst.close()

        tasks = [
            asyncio.create_task(forward(reader, up_writer)),
            asyncio.create_task(forward(up_reader, writer)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def start_socks5_auth_relay(server: str, username: str, password: str) -> Socks5AuthRelay:
    """解析 proxy dict 的 server(socks5://host:port) 并启动认证中继。"""
    raw = server[len("socks5://"):] if server.startswith("socks5://") else server
    host, _, port = raw.rpartition(":")
    if not host or not port:
        raise ValueError(f"无法解析 socks5 server: {server}")
    relay = Socks5AuthRelay(host, int(port), username, password)
    return await relay.start()
