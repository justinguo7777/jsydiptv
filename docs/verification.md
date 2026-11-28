# jsydiptv 回归验收清单

在隔离 fnOS 测试机执行装机与更新。不要在生产上试装、制造故障或卸载；历史安装包也不自动成为回滚包。任一必验项未通过，不得把产物标为已验收。

## A. 源码（本地、离线）

构建依赖见 [开发与打包](development.md)。使用 `--browser` 前安装 Playwright 与 Chromium（`python3 -m pip install playwright`、`python3 -m playwright install chromium`）。

- [ ] `fpk/manifest` 的版本和更新说明符合本次候选版本；`fpk/wizard/install` 只需 `wizard_port`，地市等由飞牛应用设置配置。
- [ ] JSON 配置可解析，UI 入口引用的向导字段存在；Python、fnOS Shell 回调及前端 JavaScript 语法检查通过。
- [ ] 离线回归通过：`python3 -B -m unittest discover -s verify -p 'test_*.py'`。不连接 IPTV 或修改现有服务。

- [ ] 从测试包跑本地合成数据 UI/HTTP：`python3 -B verify/local_smoke.py --fpk "$TEST_FPK" --browser`。检查浅色/深色、长频道名、320–1440px、切台/停止打断迟到回看、设置草稿与保存后的清单应用。

## B. 安装包与构建指纹

- [ ] 在干净、独立的构建目录打包。根层为 `ICON.PNG`、`ICON_256.PNG`、`manifest`、`app.tgz`、`cmd/`、`config/`、`wizard/`；`app.tgz` 根层是 `server/`、`ui/`、`www/`，不套 `app/`，不包含缓存或运行日志。
- [ ] 对照构建提交逐字节核对包内文件；生成的 `www/VERSION` 等于 manifest 版本。检查图标、许可文件、向导、回调及权限。
- [ ] 实算 `sha256sum "$TEST_FPK"`，与同批 `build.json` 相符；将 SHA-256、版本、Commit、测试机版本和日期写入验收记录。同一 SHA-256 贯穿装机、实播和发布；重打包后重新核对。

## C. 安装、升级与生命周期

- [ ] 验证全新安装和从已验旧版升级。在应用中心和 `/api/status` 分别读回目标版本，不能只看 `running`。配置、旧频道产物仍在；下一次成功更新后记录新 `meta.generation`。
- [ ] 验证桌面 iframe 实际端口（默认端口和自定义端口各一次），没有 `${wizard_port}` 字面量。首页、两份 `workspace.*` 和 `hls.min.js` 可读；右上角版本等于 `/api/status.version`，并与安装包 manifest 一致。
- [ ] 连续启停三轮：每次启动都确认 HTTP 就绪及运行版本；每次停止都确认应用进程、监听和旧连接释放。无更新时三轮的产物代号不变。重复点击启动不得杀掉已运行的服务。
- [ ] 仅对隔离机演练卸载与回装，确认端口和进程释放，同时按产品约定验证用户配置、数据是否保留。回退到 1.7.2 这类旧服务可能会读取 `data/` 中保留的旧布局，不能假定它会识别 `.current`；回退后检查时间、频道及 EPG，必要时重新更新。

## D. 数据、界面与真实播放

设置测试 URL（只使用隔离机），验证 `minimal/full × on/off` 四种组合。每次改设置后先手动执行一次成功更新，再运行对应命令；额外验证完整→精简、EPG 开→关→开。

```bash
python3 -B verify/round1_api.py --base-url "$TEST_URL" --version "$VERSION" --mode minimal --epg on
# 将 --mode / --epg 替换成各次测试的实际设置；验证重启保留可加 --generation。
```

- [ ] API 检查通过：智能、原版、自定义三套清单、Kodi 格式、FongMi 配置与按日 EPG、EPG 关闭时的空表与不附带 EPG 地址、完整/精简实际产物一致。频道数按当前上游数据，不用历史固定值断言。
- [ ] 在测试首页执行 `verify/round2_ui.js`，记录 `ok` 和失败项。避开正在更新和人工筛选列表的时段。
- [ ] 选两组普通直播和一组高画质台，实际确认连续画面与声音；在支持与不支持 HEVC 的浏览器分别验证。未测到的频道不显示真 4K；有合适直播备选档才可降档。
- [ ] 对照节目单选已播节目，确认网页回看内容及时间段。刷新当前节目或遇到降档提示时，网页不得切回直播。
- [ ] 外部播放器按官方文档和源码核对实际产物。FongMi 使用 `fongmi.json`、`fongmi.m3u` 和按日 JSON 节目单，Kodi IPTV Simple 使用 `kodi.m3u` 和 XMLTV。核对频道 ID、各自的时间占位符及 `catchup="default"` 的完整回看路径，并抽查展开后的回看首段时间。外部播放器真机试播可单独记录。参考 [FongMi 配置](https://fongmi.github.io/TV/config/) 和 [Kodi 回看配置](https://github.com/kodi-pvr/pvr.iptvsimple#catchup-format-specifiers)。
- [ ] 跨午夜但未执行次日定时更新时，FongMi 可读上一代提前生成的当天数据；保留至少一天的未来抓取窗口。确认同时打开的网页节目单在新一代到达后刷新。
- [ ] 在隔离测试中模拟本轮更新失败，确认 `meta.generation`、清单、EPG 和台标仍来自上一代，页面显示「上次更新失败」。成功更新后再确认各端继续可读。
- [ ] 如需跨信任域开放，单独验收管理鉴权和媒体请求各跳目标限制。本清单不能替代访问边界验证。

## E. 记录与失败处理

记录字段：`版本 / SHA-256 / 构建提交 / fnOS / 播放器及版本 / 模式与 EPG / 产物代号 / 测试结果 / 证据 / 回退结果`。

失败时停止后续发布，保留失败日志和包哈希，只在已授权的隔离机按事先验证的回退流程恢复；不要用未验旧包覆盖在用环境。
