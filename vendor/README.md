# 侧车源码快照

`kgcheckin-src.tar.gz` 是插件 bootstrap 时解压的 Node 源码包，**不含 node_modules**，依赖由 `npm ci --omit=dev` 现场安装。

## 来源

- 上游项目：[**develop202/kgcheckin**](https://github.com/develop202/kgcheckin)（酷狗概念 VIP 自动签到，GitHub Actions 版）
- 快照直接从原仓库 `main` 分支打包，`api/` 层来自 [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)

开发期间用的 `irislilymoon/kgcheckin` 只是原仓库的同源副本，两者代码逐文件比对**完全一致**，所以快照统一取自原仓库，不再引用 fork。

## 与上游的差异

只删除了两类与运行无关的内容，代码文件未改动：

- `imgs/`（README 用的截图，约 450 KB）
- `.github/`（Actions workflow，迁到本地后不再使用）

## 为什么内置而不是每次下载

- 国内网络访问 `codeload.github.com` 经常超时，内置快照让首次安装不依赖 GitHub 可达性
- 上游一改代码，补丁可能失配；内置快照保证"装得上"这件事可复现

想强制从上游拉取最新代码时，把插件配置里的 `source_url` 填成：

```
https://codeload.github.com/develop202/kgcheckin/tar.gz/refs/heads/main
```

再点面板「初始化 / 重建侧车」，会优先下载该地址、失败自动回落到内置快照。

## 重新打包

```bash
curl -sSL -o kg.tar.gz https://codeload.github.com/develop202/kgcheckin/tar.gz/refs/heads/main
rm -rf src && mkdir src && tar xzf kg.tar.gz -C src --strip-components=1
cd src && tar czf ../kgcheckin-src.tar.gz --exclude=./imgs --exclude=./.github .
```

替换本目录下的 `kgcheckin-src.tar.gz`，再在面板点「初始化 / 重建侧车」。
补丁由 `kugou/patch_sidecar.py` 自动打，若上游改了被替换的代码片段，会直接报 `[FAIL]` 并指出文件，不会留半套补丁。

## 版权

`api/` 目录自带 MIT LICENSE（来自 KuGouMusicApi）。上游根目录未附 LICENSE，Node 代码版权归 develop202 所有；本仓库的 MIT 许可只覆盖插件自身的 Python 代码与面板。
