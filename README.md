# 酷狗签到（AstrBot 插件）

酷狗概念版 VIP 每日签到。Python 只负责调度、状态机、消息交互与持久化；真正的领取逻辑由上游 Node 代码作为**侧车进程**执行。

签到与登录逻辑来自 **[develop202/kgcheckin](https://github.com/develop202/kgcheckin)**（GitHub Actions 版酷狗概念 VIP 自动签到），其 `api/` 层来自 [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)。本插件只做 AstrBot 适配（调度、登录状态机、QQ 推送、面板），不重写协议。

## 安装（3 分钟）

**前置条件**：AstrBot 4.29+，本机有 Node 20 以上（`node -v` 能输出版本号）。

1. AstrBot 面板 → 插件 → 从仓库安装，填：
   ```
   https://github.com/irislilymoon/astrbot_plugin_kugou_checkin
   ```
2. 启用插件，侧栏出现「酷狗签到」页签
3. 打开面板，点 **初始化 / 重建侧车**，等 1-3 分钟（解压源码 + `npm ci` + 打补丁）
4. 登录：点「生成二维码」用酷狗音乐 APP 扫，或在 QQ 里发 `/酷狗登录 13800138000`
5. 在 QQ 私聊里发一次 `/酷狗签到` —— 会自动把当前会话设为推送目标
6. 需要每天自动跑，再打开面板里的「每日定时」

没装 Node 会提示「找不到可用的 Node」，装完 Node 或在插件配置里填 `node_path` 即可。

![面板](docs/panel.png)

## 为什么不重写成 Python

上游 `api/` 是完整的酷狗客户端协议实现（请求签名、加密、pako 解压），重写等于复刻整个协议，上游一改就失效。所以：

- Node 代码不打包进插件包（全量依赖 277 个包、约 122 MB）
- 首次启用时 bootstrap：解压源码快照 → `npm ci --omit=dev` → 打补丁 → `node --check`
- 配置项 `sidecar_dir` 可指向已有代码目录，跳过 bootstrap
- 配置项 `source_url` 可让 bootstrap 改从上游下载最新代码，失败自动回落内置快照

## 关键约束

| 约束 | 原因 |
|---|---|
| 子进程必须 `TZ=UTC` | `main.js` 里有 `+8h` 补偿，是给 GitHub Actions 的 UTC 环境写的。给 `Asia/Shanghai` 会加两次，日期错一天，周六 16:00 后还会被误判成周日触发 token 续期 |
| 与服务器版互斥 | 两边同时跑会争抢当天额度，后跑的撞上 `30002` 次数已用光，表现为伪失败 |
| 不承诺每天必达 | 腾讯对 QQ 主动消息有额度与频次限制，被拒时只在日志留原因，面板仍可查结果 |

## 目录

```
astrbot_plugin_kugou_checkin/
├── metadata.yaml
├── _conf_schema.json
├── main.py                 # 指令、Web API、生命周期
├── kugou/
│   ├── sidecar.py          # bootstrap、进程管理、stdout 解析
│   ├── patch_sidecar.py    # 本地存储补丁 + Windows 进程管理补丁
│   ├── store.py            # userinfo / history / state / bindings
│   ├── login.py            # 扫码与手机号两条状态机
│   ├── scheduler.py        # 每日定时与补跑
│   └── notify.py           # 结果格式化（通知只走 QQ）
├── pages/checkin/index.html
└── vendor/kgcheckin-src.tar.gz   # 上游源码快照（不含 node_modules）
```

数据目录：`<AstrBot 实例>/data/plugin_data/astrbot_plugin_kugou_checkin/`

| 文件 | 内容 |
|---|---|
| `kgcheckin/` | 侧车代码（bootstrap 产物） |
| `userinfo.json` | `[{"userid": ..., "token": "..."}]`，权限尽力设 600 |
| `history.jsonl` | 每次运行一行 |
| `state.json` | `last_run_date`、`last_result`、`tokens` 等 |
| `bindings.json` | 推送目标会话 umo |
| `qr/` | 二维码临时文件，登录完成后清理 |

## 指令

| 指令 | 说明 |
|---|---|
| `/酷狗签到` | 立即执行一次，约 4 分钟/账号 |
| `/酷狗状态` | 今日状态、VIP 到期、下次运行、token 剩余 |
| `/酷狗登录 扫码` | 生成二维码并发送图片 |
| `/酷狗登录 13800138000` | 发送短信验证码 |
| `/酷狗登录 验证码 123456` | 用验证码换 token |
| `/酷狗绑定` | 把当前 QQ 会话设为推送目标 |
| `/酷狗历史 [天数]` | 默认 7 天 |

## 通知：只走 QQ

本插件跑在 AstrBot 里，通知就复用 AstrBot 已连接的 QQ 平台，**不接企业微信 / Server酱 / 邮件等外部渠道**。

- 在 QQ 里对机器人发一次 `/酷狗绑定`，把当前会话的 `unified_msg_origin` 写进 `bindings.json`
- 之后每次签到（手动、面板、定时）都把结果推到这些会话
- 触发本次运行的会话不会重复推——那边已经直接回了结果
- 单个会话推送失败只记原因，不影响其他会话，也不会让签到本身失败

QQ 官方平台对主动消息有额度与频次限制，发不出去时日志里会有 `QQ 推送失败` 的原因；结果本身仍然写进 `history.jsonl`，面板随时能查。

## 面板

`pages/checkin/index.html`，AstrBot 自动生成侧栏页签。页面通过 `window.AstrBotPluginView` 的 postMessage bridge 调插件 Web API——dashboard 用 `sandbox="allow-scripts allow-forms allow-downloads"` 渲染插件页，没有 `allow-same-origin`，页面处于匿名源，不能用 ES module 跨目录 import，也不能依赖 cookie。

## Windows 适配（与服务器版的差异）

上游 `utils/utils.js` 有三处只能在 Linux 跑：

1. `spawn('npm', ['run','apiService'])` 在 Windows 上 ENOENT（npm 是 `npm.cmd`，不经 shell 解析不到）
   → 不再经过 npm，直接 `spawn(process.execPath, ['app.js'], { cwd: 'api' })`。
   顺带解决两件事：少一层 npm/cmd 进程，冷启动快约 1 秒；退出时不用再杀 npm 进程树。
2. `close_api()` 用 `process.kill(-pid)` 杀进程组，Windows 不支持负 PID，回退 `api.kill()` 只杀掉直接子进程，Express 会留在 3000 端口 → 改为 `taskkill /PID <pid> /T /F`
3. AstrBot Launcher 的 `components/nodejs/shims/npm.cmd` 会强制注入
   `NPM_CONFIG_NPM_GLOBALCONFIG`，npm 11 把它解析成未知配置并打印
   `npm warn Unknown env config "npm-globalconfig"`。
   → 插件侧两处处理：启动子进程时清掉 `npm_config_npm_*`；bootstrap 时优先用真 npm
   （`shims` 的上一级 `npm.cmd`）绕开 shim。

这三处由 `kugou/patch_sidecar.py` 在 bootstrap 时自动打上，失败立即 `[FAIL]`，不会留半套补丁。
补丁支持从上一版升级（npm.cmd 启动方式 → node 直启），不需要手动删代码目录。

## 已知风险

| 风险 | 应对 |
|---|---|
| 上游代码变更导致补丁失配 | 补丁逐片段精确匹配，失配即报错并指出文件；不会静默跳过 |
| Windows 子进程中文输出乱码 | 按 UTF-8 解码，失败回退 GBK；解析前剥 ANSI 色码 |
| Node 版本过低 | 启动前 `node -v` 校验，低于 20 直接拒绝 |
| 3000 端口被占 | 运行前探测并指出占用 PID |
| token 过期 | 签到前探活（`token过期或账号不存在`）+ 面板剩余天数预警 |

## 参考与致谢

| 项目 | 作用 |
|---|---|
| [develop202/kgcheckin](https://github.com/develop202/kgcheckin) | 酷狗概念 VIP 自动签到的 Node 实现，本插件的侧车代码（`main.js` / `qrcodeLogin.js` / `phoneLogin.js` / `sent.js` / `utils/` / `api/`）直接来自这里 |
| [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi) | `api/` 目录的酷狗客户端协议实现（请求签名、加密、接口封装） |

本插件在两者之上只做了这些改动：

- `vendor/kgcheckin-src.tar.gz`：去掉 `imgs/`、`.github/` 的源码快照，供 bootstrap 解压
- 本地存储补丁：把「写回 GitHub Secret」换成写本地 `userinfo.json`
- Windows 进程管理补丁：`npm run apiService` 改为直接 `node app.js`，`process.kill(-pid)` 改为 `taskkill /T /F`
- Python 侧：调度、登录状态机、QQ 推送、面板、持久化

上游代码的版权与免责声明归原作者所有。本项目仅供学习研究，请勿用于商业用途，音乐平台不易，请支持正版。

## 署名

`irislilymoon`（`metadata.yaml` 的 `author` 与 `main.py` 的 `@register` 第二参数保持一致）
