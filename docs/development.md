# 开发与打包

- `fpk/app/server/`：标准库 HTTP 服务、抓取、分组、节目单与媒体探测。
- `fpk/app/www/`：观看、订阅和设置界面；hls.js 的版本与许可见 [第三方说明](../third-party/hls.js/README.md)。
- `fpk/app/ui/`、`fpk/cmd/`、`fpk/config/`、`fpk/wizard/`：飞牛桌面入口、应用生命周期、权限和向导。
- `verify/`：离线测试、合成数据冒烟及 API、浏览器检查；见 [验收清单](verification.md)。

## 构建

在 Linux 环境、干净的 Git checkout 中使用 Bash、Git、Python 3.8+、GNU tar 和 GNU coreutils：

```bash
bash tools/build.sh --check
bash tools/build.sh
```

使用 `PYTHON` 指定 Python 解释器（默认 `python3`）；安装了 Node.js 时也会检查 JavaScript 语法。浏览器测试另需 Playwright 和 Chromium。脚本会核对 Git 索引中的 FPK 文件清单，拒绝未登记的包内文件。构建输出到 `dist/<版本>/`，已有同版本目录时不会覆盖。`build.json` 记录提交、Git 工作区状态、构建时间戳和 FPK 的 SHA-256。对外分发应从干净的源码提交构建，不使用工作区临时修改。

FPK 根层为 `manifest`、两个 `ICON*.PNG`、`app.tgz` 及 `cmd/`、`config/`、`wizard/`。`app.tgz` 直接包含 `etc/`、`server/`、`ui/`、`www/`。`www/VERSION`、应用 Logo 和许可文件在构建时加入。复核已发布的附件时，请使用对应标签与发布页的 SHA-256。

正式版固定使用 `1.7.x`，每次发布只递增末段，例如 `1.7.4` → `1.7.5`。

## 装机边界

网页设置和飞牛应用设置是两个入口。当前版在安装时注册桌面端口；安装后改端口可能需要重装以重新注册入口。当前 manifest 对默认端口 `18123` 声明了飞牛端口预检，不能把它当成对自定义端口的验证。默认端口和自定义端口都应在隔离机验收。网页服务仅面向受信任局域网。
