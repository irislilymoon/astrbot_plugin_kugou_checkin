# -*- coding: utf-8 -*-
"""对上游 kgcheckin 源码打补丁。

两类改动：
1. 本地存储补丁（沿用服务器版 patch_kg.py 的 4 处）：把「写回 GitHub Secret」换成
   「写本地文件」，未设置 KG_USERINFO_FILE 时仍回落到原逻辑，不破坏上游 Actions 用法。
2. Windows 兼容补丁（服务器版没有）：上游 startService 直接 spawn('npm') 在 Windows
   上会 ENOENT；close_api 用 process.kill(-pid) 在 Windows 上抛错，回退只会杀掉 npm，
   Express 会留在 3000 端口。改为 npm.cmd + shell + taskkill /T。

任何一处匹配失败立即抛 PatchError，不做半套补丁。所有改动文件留 .bak。
"""

from __future__ import annotations

import shutil
from pathlib import Path

LOCAL_STORE_JS = """import fs from 'node:fs'
import path from 'node:path'

function storeFile() {
  return process.env.KG_USERINFO_FILE || ''
}

function localStoreEnabled() {
  return Boolean(storeFile())
}

function readUserinfo() {
  const file = storeFile()
  if (!file) return null
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'))
    return Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

function writeUserinfo(userinfo) {
  const file = storeFile()
  if (!file) return false
  if (!Array.isArray(userinfo)) return false

  const dir = path.dirname(file)
  fs.mkdirSync(dir, { recursive: true })

  const tmp = `${file}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(userinfo, null, 2), { encoding: 'utf8', mode: 0o600 })
  fs.renameSync(tmp, file)
  try {
    fs.chmodSync(file, 0o600)
  } catch {}
  return true
}

export { localStoreEnabled, readUserinfo, writeUserinfo }
"""


class PatchError(RuntimeError):
    pass


def _patch_file(root: Path, rel: str, pairs: list[tuple[str, str]]) -> None:
    path = root / rel
    if not path.is_file():
        raise PatchError(f"缺少文件 {rel}，上游结构可能已变")
    text = path.read_text(encoding="utf-8")
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)

    for old, new in pairs:
        if new in text:
            continue  # 已打过
        count = text.count(old)
        if count == 0:
            raise PatchError(f"{rel}: 未找到目标片段 -> {old.strip().splitlines()[0][:80]!r}")
        if count > 1:
            raise PatchError(f"{rel}: 片段出现 {count} 次，预期 1 次")
        text = text.replace(old, new)

    path.write_text(text, encoding="utf-8")


def apply_local_store(root: Path) -> None:
    """4 处本地存储补丁，与服务器版 patch_kg.py 等价。"""
    store = root / "utils" / "localStore.js"
    if not store.exists():
        store.write_text(LOCAL_STORE_JS, encoding="utf-8")

    _patch_file(
        root,
        "main.js",
        [
            (
                'import { close_api, delay, send, startService, waitForApi } from "./utils/utils.js";',
                'import { close_api, delay, send, startService, waitForApi } from "./utils/utils.js";\n'
                'import { readUserinfo, writeUserinfo } from "./utils/localStore.js";',
            ),
            (
                "  const USERINFO = process.env.USERINFO\n  // 刷新token",
                "  // 服务器模式：环境变量未提供时回落到本地文件（KG_USERINFO_FILE）\n"
                "  const USERINFO = process.env.USERINFO || (() => {\n"
                "    const local = readUserinfo()\n"
                "    return local ? JSON.stringify(local) : ''\n"
                "  })()\n"
                "  // 刷新token",
            ),
            (
                "  let secretError = null\n  if (needRefresh) {\n    if (hasSecretWriteToken()) {",
                "  let secretError = null\n  if (needRefresh) {\n"
                "    if (writeUserinfo(userinfo)) {\n"
                '      printGreen("本地 userinfo 文件 token 刷新成功")\n'
                "    } else if (hasSecretWriteToken()) {",
            ),
        ],
    )

    _patch_file(
        root,
        "utils/userinfo.js",
        [
            (
                "import { hasSecretWriteToken, setRepoSecret } from './githubSecrets.js'",
                "import { hasSecretWriteToken, setRepoSecret } from './githubSecrets.js'\n"
                "import { writeUserinfo } from './localStore.js'",
            ),
            (
                "  const userinfoJSON = JSON.stringify(userinfo)\n\n  if (hasSecretWriteToken()) {",
                "  const userinfoJSON = JSON.stringify(userinfo)\n\n"
                "  if (writeUserinfo(userinfo)) {\n"
                "    printGreen('本地 userinfo 文件写入成功')\n"
                "    return\n"
                "  }\n\n"
                "  if (hasSecretWriteToken()) {",
            ),
        ],
    )

    for rel in ("phoneLogin.js", "qrcodeLogin.js"):
        _patch_file(
            root,
            rel,
            [
                (
                    "  const USERINFO = process.env.USERINFO\n  const APPEND_USER = process.env.APPEND_USER",
                    "  const USERINFO = process.env.USERINFO || (() => {\n"
                    "    const local = readUserinfo()\n"
                    "    return local ? JSON.stringify(local) : ''\n"
                    "  })()\n"
                    "  const APPEND_USER = process.env.APPEND_USER",
                ),
            ],
        )

    # 两个登录脚本需要 import readUserinfo
    for rel in ("phoneLogin.js", "qrcodeLogin.js"):
        path = root / rel
        text = path.read_text(encoding="utf-8")
        if "readUserinfo" in text and "localStore.js" not in text:
            marker = 'import { close_api, delay, send, startService, waitForApi } from "./utils/utils.js";'
            if marker not in text:
                raise PatchError(f"{rel}: 未找到 utils 导入行")
            text = text.replace(
                marker,
                marker + '\nimport { readUserinfo } from "./utils/localStore.js";',
                1,
            )
            path.write_text(text, encoding="utf-8")


def _replace_start_service(root: Path) -> None:
    """把 startService 改成直接 spawn node，不经过 npm。

    原因：
      1. 少一层 npm/cmd 进程，冷启动快约 1 秒，退出时也少一棵进程树；
      2. AstrBot Launcher 的 npm shim 会注入 npm_config_npm_globalconfig，
         npm 11 把它当成未知配置，每次运行都打印
         `Unknown env config "npm-globalconfig"`。直接起 node 就没有这条路径。

    兼容三种状态：已升级 / 上一版 npm.cmd 补丁 / 上游原文。
    """
    path = root / "utils" / "utils.js"
    text = path.read_text(encoding="utf-8")

    new_block = (
        "function startService() {\n"
        "  // 直接起 node，不再经过 npm：少一层进程，也避开 AstrBot Launcher 的 npm shim\n"
        "  // 注入 npm_config_npm_globalconfig（npm 11 会报 Unknown env config）。\n"
        "  const apiDir = path.join(process.cwd(), 'api')\n"
        "  const api = spawn(process.execPath, ['app.js'], {\n"
        "    cwd: apiDir,\n"
        "    detached: true,\n"
        "    stdio: ['ignore', 'pipe', 'pipe'],\n"
        "    windowsHide: true,\n"
        "  })"
    )

    if new_block in text:
        return

    candidates = [
        # 上一版：npm.cmd + shell
        "function startService() {\n"
        "  // Windows 下 npm 是 npm.cmd，不经 shell 直接 spawn 会 ENOENT。\n"
        "  const isWin = process.platform === 'win32'\n"
        "  const api = spawn(isWin ? 'npm.cmd' : 'npm', ['run', 'apiService'], {\n"
        "    detached: true,\n"
        "    stdio: ['ignore', 'pipe', 'pipe'],\n"
        "    shell: isWin,\n"
        "    windowsHide: true,\n"
        "  })",
        # 上游原文
        "function startService() {\n"
        "  const api = spawn('npm', ['run', 'apiService'], {\n"
        "    detached: true,\n"
        "    stdio: ['ignore', 'pipe', 'pipe'],\n"
        "  })",
    ]
    for old in candidates:
        if old in text:
            text = text.replace(old, new_block, 1)
            break
    else:
        raise PatchError("utils/utils.js: 未找到 startService 片段，上游结构可能已变")

    path.write_text(text, encoding="utf-8")


def _ensure_imports(root: Path) -> None:
    """保证 utils.js 里有 spawn / spawnSync / path 三个依赖。

    兼容三种状态：已升级 / 上一版只补了 spawnSync / 上游原文。
    """
    path = root / "utils" / "utils.js"
    text = path.read_text(encoding="utf-8")

    if "import path from 'node:path'" in text:
        if "spawnSync" not in text.split("\n")[0]:
            raise PatchError("utils/utils.js: 已有 path 导入但缺少 spawnSync")
        return

    if "import { spawn, spawnSync } from 'child_process'" in text:
        text = text.replace(
            "import { spawn, spawnSync } from 'child_process'",
            "import { spawn, spawnSync } from 'child_process'\nimport path from 'node:path'",
            1,
        )
    elif "import { spawn } from 'child_process'" in text:
        text = text.replace(
            "import { spawn } from 'child_process'",
            "import { spawn, spawnSync } from 'child_process'\nimport path from 'node:path'",
            1,
        )
    else:
        raise PatchError("utils/utils.js: 未找到 child_process 导入行")
    path.write_text(text, encoding="utf-8")


def apply_windows_fix(root: Path) -> None:
    """进程管理补丁：node 直接启动 + Windows 下 taskkill 杀进程树。"""
    _ensure_imports(root)
    _patch_file(
        root,
        "utils/utils.js",
        [
            (
                "function close_api(api) {\n"
                "  if (!api || !api.pid) return\n"
                "  try {\n"
                "    process.kill(-api.pid, 'SIGKILL') // 杀掉整个进程组（npm + Express）\n"
                "  } catch (e) {\n"
                "    try { api.kill('SIGKILL') } catch (_) { /* 已退出 */ }\n"
                "  }\n"
                "}",
                "function close_api(api) {\n"
                "  if (!api || !api.pid) return\n"
                "  // Windows 没有进程组信号：process.kill(-pid) 会抛错，回退 api.kill() 只杀 cmd/npm，\n"
                "  // 真正的 Express 会留在 3000 端口。必须用 taskkill /T 杀整棵树。\n"
                "  if (process.platform === 'win32') {\n"
                "    try {\n"
                "      spawnSync('taskkill', ['/PID', String(api.pid), '/T', '/F'], { windowsHide: true })\n"
                "    } catch (e) {\n"
                "      try { api.kill('SIGKILL') } catch (_) { /* 已退出 */ }\n"
                "    }\n"
                "    return\n"
                "  }\n"
                "  try {\n"
                "    process.kill(-api.pid, 'SIGKILL') // 杀掉整个进程组（npm + Express）\n"
                "  } catch (e) {\n"
                "    try { api.kill('SIGKILL') } catch (_) { /* 已退出 */ }\n"
                "  }\n"
                "}",
            ),
        ],
    )
    _replace_start_service(root)


def apply_all(root: Path) -> list[str]:
    root = Path(root)
    if not (root / "main.js").is_file():
        raise PatchError(f"{root} 下没有 main.js，不是 kgcheckin 目录")
    apply_local_store(root)
    apply_windows_fix(root)
    return ["localStore 本地存储补丁", "Windows 进程管理补丁"]
