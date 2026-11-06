# -*- coding: utf-8 -*-
"""Node 侧车：bootstrap、进程管理、stdout 解析。

为什么不在 Python 里重写酷狗接口：上游 api/ 是完整的客户端协议实现（请求签名、
加密、pako 解压），重写等于复刻整个协议，上游一改就失效。这里只做调度。

为什么 TZ=UTC：main.js 里有 today.setTime(+8h) 的补偿，是给 GitHub Actions 的 UTC
环境写的。子进程给 Asia/Shanghai 会加两次，日期错一天，且周六 16:00 后会被判成
周日误触发 token 续期。定时判断由 Python 侧按北京时间做，与子进程时区无关。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import socket
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .patch_sidecar import PatchError, apply_all

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MAX_LINES = 5000


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def _decode(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", "replace")
    return text


@dataclass
class RunResult:
    code: int
    lines: list[str] = field(default_factory=list)
    timed_out: bool = False
    error: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.lines[-n:])

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)


@dataclass
class CheckinSummary:
    nickname: str = ""
    listen: str = "未知"
    vip_count: int = 0
    vip_total: int = 8
    vip_expire: str = "未知"
    date: str = ""
    token_invalid: bool = False
    quota_used: bool = False
    port_conflict: bool = False
    api_error: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "nickname": self.nickname,
            "listen": self.listen,
            "vip_count": self.vip_count,
            "vip_total": self.vip_total,
            "vip_expire": self.vip_expire,
            "date": self.date,
            "token_invalid": self.token_invalid,
            "quota_used": self.quota_used,
            "port_conflict": self.port_conflict,
            "api_error": self.api_error,
            "errors": self.errors,
        }


def parse_checkin(lines: list[str]) -> CheckinSummary:
    """解析 main.js 的 stdout。上游用 console.log('%s') 带 ANSI 色码，先剥色。"""
    s = CheckinSummary()
    for raw in lines:
        line = strip_ansi(raw).strip()
        if not line:
            continue

        if m := re.search(r"账号\s+(\S+)\s+开始领取VIP", line):
            s.nickname = m.group(1)
        elif "听歌领取成功" in line:
            s.listen = "成功"
        elif "今日已领取" in line:
            s.listen = "今日已领取"
        elif "听歌领取失败" in line:
            s.listen = "失败"
            s.errors.append("听歌领取失败")
        elif m := re.search(r"第(\d+)次领取成功", line):
            s.vip_count = max(s.vip_count, int(m.group(1)))
        elif m := re.search(r"第(\d+)次领取失败", line):
            s.errors.append(f"第{m.group(1)}次领取失败")
        elif "今天次数已用光" in line:
            s.quota_used = True
        elif m := re.search(r"VIP到期时间：(.+)$", line):
            s.vip_expire = m.group(1).strip()
        elif m := re.search(r"今天是：(\d{4}-\d{2}-\d{2})", line):
            s.date = m.group(1)
        elif "token过期或账号不存在" in line:
            s.token_invalid = True
        elif "EADDRINUSE" in line:
            s.port_conflict = True
        elif "本地 API 服务在" in line and "未就绪" in line:
            s.api_error = True
            s.errors.append("本地 API 服务未就绪")
        elif "未配置" in line and not s.nickname:
            s.errors.append("未配置登录态")

    return s


class SidecarError(RuntimeError):
    pass


class Sidecar:
    def __init__(self, data_dir: Path, plugin_dir: Path, userinfo_path: Path, config, log) -> None:
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)
        self.userinfo_path = Path(userinfo_path)
        self.config = config
        self.log = log
        self._boot_lock = asyncio.Lock()
        self.boot_log: list[str] = []
        self.boot_running = False
        self.boot_ok = False
        self.last_error = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel = False
        self._npm_env_noted = False

    # ---------- 路径 ----------

    @property
    def code_dir(self) -> Path:
        custom = str(self.config.get("sidecar_dir") or "").strip()
        if custom:
            return Path(custom).expanduser()
        return self.data_dir / "kgcheckin"

    @property
    def marker(self) -> Path:
        return self.code_dir / ".bootstrap_ok"

    @property
    def node_exe(self) -> str:
        custom = str(self.config.get("node_path") or "").strip()
        if custom:
            return custom
        return shutil.which("node") or "node"

    def is_ready(self) -> bool:
        return (
            (self.code_dir / "main.js").is_file()
            and (self.code_dir / "node_modules").is_dir()
            and (self.code_dir / "api" / "node_modules").is_dir()
        )

    # ---------- 日志 ----------

    def _say(self, msg: str) -> None:
        self.boot_log.append(msg)
        if len(self.boot_log) > 400:
            del self.boot_log[:100]
        try:
            self.log.info(f"[kgcheckin] {msg}")
        except Exception:
            pass

    # ---------- 环境校验 ----------

    async def node_version(self) -> tuple[int, str]:
        code, out = await self._exec([self.node_exe, "-v"], cwd=self.plugin_dir, timeout=20)
        text = strip_ansi(out).strip()
        m = re.search(r"v(\d+)\.", text)
        if code != 0 or not m:
            raise SidecarError(f"无法执行 node（{self.node_exe}）：{text[:200]}")
        return int(m.group(1)), text

    # ---------- bootstrap ----------

    async def ensure(self, force: bool = False) -> dict:
        async with self._boot_lock:
            if not force and self.is_ready() and self.marker.exists():
                self.boot_ok = True
                return {"ok": True, "log": ["侧车已就绪"]}
            self.boot_running = True
            self.boot_log = []
            try:
                await self._bootstrap(force=force)
                self.boot_ok = True
                self.last_error = ""
                return {"ok": True, "log": list(self.boot_log)}
            except Exception as e:
                self.boot_ok = False
                self.last_error = str(e)
                self._say(f"[FAIL] {e}")
                return {"ok": False, "log": list(self.boot_log), "error": str(e)}
            finally:
                self.boot_running = False

    async def _bootstrap(self, force: bool = False) -> None:
        major, ver = await self.node_version()
        self._say(f"Node 版本：{ver}")
        if major < 20:
            raise SidecarError(f"Node {ver} 过低，本项目需要 20 及以上")

        custom = bool(str(self.config.get("sidecar_dir") or "").strip())
        if custom:
            self._say(f"使用外部代码目录：{self.code_dir}")
            if not (self.code_dir / "main.js").is_file():
                raise SidecarError(f"{self.code_dir} 下没有 main.js")
        else:
            if force or not (self.code_dir / "main.js").is_file():
                await self._extract_source()

        await self._npm_install(custom)
        self._say("应用代码补丁…")
        try:
            applied = await asyncio.to_thread(apply_all, self.code_dir)
        except PatchError as e:
            raise SidecarError(f"补丁失配：{e}（上游结构可能已变，需人工处理）")
        for item in applied:
            self._say(f"[OK] {item}")

        await self._syntax_check()
        self.marker.write_text(
            f'{{"time": {int(time.time())}, "node": "{ver}"}}',
            encoding="utf-8",
        )
        self._say("bootstrap 完成")

    async def _extract_source(self) -> None:
        archive = self.plugin_dir / "vendor" / "kgcheckin-src.tar.gz"
        if not archive.is_file():
            raise SidecarError(f"缺少源码快照 {archive}")
        if self.code_dir.exists():
            self._say("清理旧的代码目录…")
            await asyncio.to_thread(shutil.rmtree, self.code_dir, True)
        self.code_dir.mkdir(parents=True, exist_ok=True)
        self._say(f"解压源码快照（{archive.name}）…")

        def _extract() -> None:
            with tarfile.open(archive, "r:gz") as tf:
                try:
                    # Python 3.12+ 支持 filter；3.14 起默认过滤，显式写更稳
                    tf.extractall(self.code_dir, filter="data")
                except TypeError:
                    tf.extractall(self.code_dir)

        await asyncio.to_thread(_extract)
        if not (self.code_dir / "main.js").is_file():
            raise SidecarError("解压后未找到 main.js，源码包结构异常")

    async def _npm_install(self, custom: bool) -> None:
        registry = "https://registry.npmmirror.com"
        api_dir = self.code_dir / "api"
        need_api = not (api_dir / "node_modules").is_dir()
        need_root = not (self.code_dir / "node_modules").is_dir()

        if custom and not need_api and not need_root:
            self._say("外部目录依赖已存在，跳过 npm install")
            return

        if need_api:
            self._say("安装 api 依赖（npm ci --omit=dev，约 1-3 分钟）…")
            code, out = await self._npm(
                ["ci", "--omit=dev", f"--registry={registry}", "--no-audit", "--no-fund"],
                cwd=api_dir,
                timeout=1200,
            )
            if code != 0:
                self._say("npm ci 失败，改用 npm install 重试…")
                code, out = await self._npm(
                    ["install", "--omit=dev", f"--registry={registry}", "--no-audit", "--no-fund"],
                    cwd=api_dir,
                    timeout=1200,
                )
            if code != 0:
                raise SidecarError(f"api 依赖安装失败：{out[-500:]}")
            self._say("[OK] api 依赖安装完成")
        else:
            self._say("api 依赖已存在，跳过")

        if need_root:
            self._say("安装根依赖（npm install --ignore-scripts）…")
            code, out = await self._npm(
                ["install", "--ignore-scripts", "--omit=dev", f"--registry={registry}", "--no-audit", "--no-fund"],
                cwd=self.code_dir,
                timeout=600,
            )
            if code != 0:
                raise SidecarError(f"根依赖安装失败：{out[-500:]}")
            self._say("[OK] 根依赖安装完成")
        else:
            self._say("根依赖已存在，跳过")

    async def _syntax_check(self) -> None:
        targets = [
            "main.js",
            "utils/localStore.js",
            "utils/userinfo.js",
            "utils/utils.js",
            "phoneLogin.js",
            "qrcodeLogin.js",
            "sent.js",
        ]
        for rel in targets:
            path = self.code_dir / rel
            if not path.is_file():
                raise SidecarError(f"语法检查缺少文件：{rel}")
            code, out = await self._exec([self.node_exe, "--check", rel], cwd=self.code_dir, timeout=60)
            if code != 0:
                raise SidecarError(f"node --check {rel} 失败：{strip_ansi(out)[-400:]}")
        self._say(f"[OK] {len(targets)} 个文件语法检查通过")

    # ---------- 进程执行 ----------

    def _npm_exe(self) -> str:
        npm = shutil.which("npm") or "npm"
        p = Path(npm)
        # AstrBot Launcher 的 shims/npm.cmd 会强制注入 npm_config_npm_globalconfig，
        # npm 11 把它当未知配置，每次调用都打印 Unknown env config 警告。
        # 这里优先用同级目录里的真 npm，绕开 shim（我们本来就用显式 --registry）。
        if p.parent.name.lower() == "shims":
            real = p.parent.parent / ("npm.cmd" if os.name == "nt" else "npm")
            if real.is_file():
                return str(real)
        return npm

    async def _npm(self, args: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        exe = self._npm_exe()
        if os.name == "nt":
            argv = ["cmd", "/c", exe, *args]
        else:
            argv = [exe, *args]
        return await self._exec(argv, cwd=cwd, timeout=timeout)

    async def _exec(self, argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._env(),
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError as e:
            return 127, f"{e}"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill_tree(proc)
            return -1, "命令超时"
        return proc.returncode or 0, _decode(out or b"")

    def _env(self, extra: dict | None = None) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "TZ": "UTC",
                "NODE_ENV": "production",
                "KG_USERINFO_FILE": str(self.userinfo_path),
                "PYTHONIOENCODING": "utf-8",
                "NODE_NO_WARNINGS": "1",
            }
        )
        env.pop("USERINFO", None)  # 避免继承到宿主环境里的旧值

        # AstrBot Launcher 的 npm shim 会注入 npm_config_npm_globalconfig，
        # npm 11 把它解析成未知配置 "npm-globalconfig" 并打印：
        #   npm warn Unknown env config "npm-globalconfig". This will stop working...
        # 只清掉这一类嵌套的非法键；cache/prefix/registry/userconfig 都是合法配置，
        # 保留宿主原意（否则 npm 会回落到 ~/.npmrc，又冒出 electron_mirror 的告警）。
        stripped = [k for k in env if k.lower().startswith("npm_config_npm_")]
        for key in stripped:
            env.pop(key, None)
        if stripped and not self._npm_env_noted:
            self._npm_env_noted = True
            self._say(f"已清理宿主非法 npm 环境变量：{', '.join(sorted(stripped))}")

        if extra:
            env.update({k: str(v) for k, v in extra.items() if v is not None})
        return env

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        """连子孙进程一起杀。

        main.js 会 spawn 一个 detached 的 api 子进程占着 127.0.0.1:3000。
        只杀 main.js 会让 api 变孤儿，端口一直不释放。Windows 用 taskkill /T，
        Linux 用进程组（run_script 启动时已 start_new_session，自成一组）。
        """
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            pass

    async def run_script(
        self,
        script: str,
        args: list[str] | None = None,
        env_extra: dict | None = None,
        timeout: int = 300,
        on_line=None,
    ) -> RunResult:
        if not self.is_ready():
            return RunResult(code=-2, error="侧车未就绪，请先执行 bootstrap")

        argv = [self.node_exe, script, *(args or [])]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.code_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=self._env(env_extra),
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError:
            return RunResult(code=127, error=f"找不到 node：{self.node_exe}")

        self._proc = proc
        lines: list[str] = []
        timed_out = False

        async def _pump() -> None:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = strip_ansi(_decode(raw)).rstrip()
                if not line:
                    continue
                if len(lines) < MAX_LINES:
                    lines.append(line)
                if on_line is not None:
                    try:
                        on_line(line)
                    except Exception:
                        pass

        try:
            await asyncio.wait_for(_pump(), timeout)
            code = await proc.wait()
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill_tree(proc)
            code = -1
        except asyncio.CancelledError:
            await self._kill_tree(proc)
            raise
        finally:
            self._proc = None

        return RunResult(code=code or 0, lines=lines, timed_out=timed_out)

    async def kill_running(self) -> None:
        if self._proc is not None:
            await self._kill_tree(self._proc)
            self._proc = None

    async def run_checkin(self, on_line=None) -> RunResult:
        timeout = int(self.config.get("checkin_timeout") or 1800)
        return await self.run_script("main.js", timeout=max(120, timeout), on_line=on_line)

    # ---------- 端口 ----------

    async def port_busy(self) -> tuple[bool, str]:
        loop = asyncio.get_running_loop()

        def _probe() -> bool:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.6)
                return s.connect_ex(("127.0.0.1", 3000)) == 0

        busy = await loop.run_in_executor(None, _probe)
        if not busy:
            return False, ""

        if os.name != "nt":
            return True, await self._port_owner_posix()

        owner = ""
        try:
            code, out = await self._exec(["netstat", "-ano"], cwd=self.plugin_dir, timeout=15)
            for line in out.splitlines():
                if ":3000" in line and "LISTENING" in line.upper():
                    owner = line.split()[-1]
                    break
            if owner:
                code, out2 = await self._exec(
                    ["tasklist", "/FI", f"PID eq {owner}", "/FO", "CSV", "/NH"],
                    cwd=self.plugin_dir,
                    timeout=15,
                )
                name = out2.strip().split(",")[0].strip('"')
                if name:
                    owner = f"PID {owner}（{name}）"
                else:
                    owner = f"PID {owner}"
        except Exception:
            pass
        return True, owner or "未知进程"

    async def _port_owner_posix(self) -> str:
        """Linux/macOS 下找出占用 3000 的进程：优先 ss，其次 lsof。"""
        try:
            code, out = await self._exec(
                ["ss", "-lntp", "sport = :3000"], cwd=self.plugin_dir, timeout=10
            )
            m = re.search(r"pid=(\d+)", out)
            if m:
                pid = m.group(1)
                _, name = await self._exec(
                    ["ps", "-p", pid, "-o", "comm="], cwd=self.plugin_dir, timeout=10
                )
                name = name.strip()
                return f"PID {pid}（{name}）" if name else f"PID {pid}"
        except Exception:
            pass
        try:
            code, out = await self._exec(
                ["lsof", "-nP", "-iTCP:3000", "-sTCP:LISTEN", "-t"],
                cwd=self.plugin_dir,
                timeout=10,
            )
            pid = out.strip().splitlines()[0] if out.strip() else ""
            if pid:
                return f"PID {pid}"
        except Exception:
            pass
        return "未知进程"
