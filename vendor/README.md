# 侧车源码快照

`kgcheckin-src.tar.gz` 是插件 bootstrap 时解压的 Node 源码包，**不含 node_modules**，依赖由 `npm ci --omit=dev` 现场安装。

## 来源

- 上游项目：[develop202/kgcheckin](https://github.com/develop202/kgcheckin)（酷狗概念 VIP 自动签到，GitHub Actions 版）
- API 层：[MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)
- 本快照取自作者维护的分支 `irislilymoon/kgcheckin`（与上游同源）

## 与上游的差异

只删除了两类与运行无关的内容，代码文件未改动：

- `imgs/`（README 用的截图，约 450 KB）
- `.github/`（Actions workflow，迁到本地后不再使用）

## 为什么内置而不是下载

上游仓库已转为 private，匿名访问 `codeload.github.com` 会返回 404，bootstrap 必然失败。内置快照让首次安装不依赖 GitHub 可达性。

## 更新方式

上游改动后重新打包：

```bash
# 在有权限的机器上
gh api repos/irislilymoon/kgcheckin/tarball/main > kg.tar.gz
mkdir -p src && tar xzf kg.tar.gz -C src --strip-components=1
cd src && tar czf ../kgcheckin-src.tar.gz --exclude=./imgs --exclude=./.github .
```

替换本目录下的 `kgcheckin-src.tar.gz`，然后在面板点「初始化 / 重建侧车」。
补丁由 `kugou/patch_sidecar.py` 自动打，若上游改了被替换的代码片段，会直接报 `[FAIL]` 并指出文件，不会留半套补丁。
