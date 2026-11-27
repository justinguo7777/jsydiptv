# 江苏移动 IPTV 应用开发报告

> 历史快照：保留当时的目录和接口描述；现行入口见 [README](../../README.md) 和 [验收清单](../verification.md)。

> 应用标识：`jsydiptv` ｜ 目标平台：飞牛 fnOS（fnnas）
> 当前版本：**1.6.0** ｜ 报告生成：2026-11-25（1.6.0 修订）
> 适用：后续版本更新的参考基线、回归验证清单、故障定位手册

---

## 0. 1.6.0 变更摘要（2026-11-25）

### 0.1 修复：点频道后右侧列表跳回顶部

根因：`playChannel()` 末尾调用 `renderLists()`，`renderGrouped()` 用 `box.innerHTML=''` 整体重建列表，
滚动条归零。修法：重建前记录 `box.scrollTop`，重建后恢复；若选中项不在视口则把它滚进视口，否则原样还原。
实测：点击后 `scrollTop` 前后一致（如 10011 → 10011）。

### 0.2 新增：一键展开/折叠全部分组

搜索栏右侧加「全部折叠 / 全部展开」按钮，统一控制所有 `.gsec`。
另加 `FOLDED{}` 记忆：单组折叠状态跨 `renderLists` 重建保留（原先每次重建就丢）。

### 0.3 分组体系重构（核心）

旧版「其他」是一坨 134 台。1.6.0 按内容拆成 6 个二级组，并与主分组统一为 11 组：

```
4K → 央视 → 卫视 → 江苏 → 地方(按市) → 影视剧场 / 少儿动画 / 体育竞技 / 生活纪录 / 音乐广播 / 购物
```

判定统一走 `_classify()`（唯一权威入口）：4K/高清臻享 → 4K；地方台(13 市清单独有) → 地方；
其余按上游 `otype` 直接映射；`专业频道/生活/空` 12 个台按频道名细分（名称规则兜底）。
关键：`专业频道` 里混着 第一剧场/风云足球/风云音乐/第一财经，不能一概归「生活纪录」，
必须按名称再判。

### 0.4 4K 组：CCTV 置顶 + 高清臻享并入

- **CCTV 台置顶并按台号升序**：`gkey` 对 4K 组给细分序号（CCTV 类 0 段按 CCTV 数字升序，央视4K 1 段，其余 2 段）。
  修了 `CCTV-164K` 被正则贪婪匹配成 164 的 bug（先剥离 4K 后缀再取号）。
- **高清臻享统一进 4K 组**（用户要求），但**不谎报真 4K**：`grp_uhd` 字段决定进组，`is_4k` 仍按实测分辨率。
  实测 9 个高清臻享里仅 CCTV1/3/5 是真 4K，CCTV2/6/8、睛彩篮球/竞技/青少 都是 1080P。
- **UHD_CODE_RE 收紧**：原正则 `(4K|UHD|2160)` 会把 AVS 编码名 `G_CCTV-1-AVS-4K264` 里的「4K264」误判成 4K，
  改为 `(?:4K|UHD|2160)(?![0-9])`。
- **4K 组同流去重**：`G_CCTV-1-AVS-4K264` 被 num=1「CCTV-1」与 num=201「CCTV1高清臻享」共用，
  按台号小的保留；被删 uuid 的节目也从 epg 中剔除（否则 epg.xml 出现孤儿 programme）。

### 0.5 央视组补齐 CCTV-1（1080P 源）

上游央视组（assort 116）的 CCTV-1 只有「标清」档，其 4K 档单独在「超高清」组 → 央视组看不到 CCTV-1。
「央视I套」(num=1000) 实为 CCTV-1 的 1080P（实测 1920×1080）源，改名「CCTV-1」放入央视组；
4K 组那份改名「CCTV-1 4K」，两者同名不同清晰度。电视端播放器按 `tvg-id`(uuid) 区分，见 §1.3。

### 0.6 探测稳定性

4K 大码率流（如 CCTV-16 8MB 才拿到 SPS）在 6 线程并发下偶发失败 → 改「并发 + 失败项串行重试」，
最终失败仍按名称兜底。`channels.json` 补齐 `w/h` 字段（原先只有 4k.json 有，界面拿不到分辨率）。

### 0.7 验收

| 轮次 | 范围 | 结果 |
|---|---|---|
| 第一轮 | 接口/产物/数据/一致性/安全（46 项） | **46/46** |
| 第二轮 | UI/交互/新功能/分组/文案（39 项） | **39/39** |
| 一致性 | channels.json ↔ epg.xml ↔ 3×m3u ↔ 4k.json 交叉核对 | 277 频道全对齐，0 孤儿 programme |
| 启停 | stop/start 3 轮 | 端口均正常释放 |

---

## 0. 1.5.0 变更摘要（2026-11-25）

### 0.1 新增：4K 分组（置顶）

| 项 | 实现 |
|---|---|
| 分组来源 | **完全来自上游** `getOpenNewAssortment`（11 个原版分组），代码不再用名字硬猜 |
| 4K 判定 | **实测**：拉候选台切片解析 SPS，3840×2160 才归入 4K 组 |
| 实测结果 | 候选 19 个 → 真 4K **15 个**（H.265 11 + H.264 4）；4 个名字带 4K 但实测 1080P 已退回原组 |
| 顺序 | `4K → 央视 → 卫视 → 江苏 → 地方-各市 → 其它` |
| 输出覆盖 | `iptv.m3u8` / `kodi.m3u` / `fongmi.m3u` / `epg.xml` 全部含 4K 分组，另出 `4k.json` |

「央视/卫视/江苏/地方」的判定链路：上游只给粗分组（分不出省级台与地市台），
故用 **13 个地市各自 assortId=120「本地」清单取交集**，交集即省级台（8 个），
差集即地市台（59 个，归入对应城市组）。

### 0.2 修复：4K 频道有声音无画面（黑屏）

三条根因，**全部实测确认**：

1. **源取错档**：原代码按 `defaultResolution` 取源，导致「江苏卫视4K」等 10 个台
   实际取的是 `H265-8000k-1080P-*` 的 **1080P 档**（名字叫 4K、播的是 1080P）。
   → 改为优先取 URL 含 4K/UHD/2160 的档。
2. **代理整段读完再回写**：4K 单切片 17–45MB，旧实现 `r.read()` 全量缓冲后一次性写出，
   前端分片请求极易超时 → 只出音轨、视频轨永远缓冲 → 纯黑。
   → 改为**流式分块转发**（8192→65536 边收边发）。
3. **不转发 Range**：播放器 seek/分段拉取时上游返回全量，时序错乱。
   → 改为 Range 透传，返回 206 + `Content-Range`。

实测对比（CCTV-16 4K，单片 25,096,496 字节）：
`64KB 小请求 0.06s` / `Range 请求 206 正确` / `全量 25MB 0.34s (71MB/s)`。

4. **缺解码器时的表现**：15 个 4K 里 11 个是 H.265，浏览器 MSE 不支持时
   只会 append 音轨。→ 起播前探测 `hevcSupported()`，不支持就直接说明原因
   并给「切到低清晰度 / 复制地址 / 外部打开」三个出口，不再静默黑屏。

### 0.3 修复：EPG 区（三项）

1. **高度不足**：原来写死 `max-height:110px` 只能看 2–3 行。
   → 改 flex 撑满剩余空间，`min-height:210px`，实测可视 **9–10 行**（行高 30px）。
2. **台标区不固定** → 滚动时内容压上来，看着像叠放。
   → `.epghead` 用 `sticky` + **不透明背景**，只有 `.plist` 自己滚；
   并用 `elementFromPoint` 做遮盖测试确认没有内容透出。
3. **回看只显示 1 条** → 原来按「当前前后 9 档」切片。
   → 改为**按天完整展示**：10 个日期按钮（前 7 天 + 今天 + 明天 + 后续），
   实测每天 **29–37 条**，已播节目全部可点回看（今天已播 16/16 可点）。

另修：头部原来是单行 flex，日期按钮会把频道副标题挤成「台号 100 · H...」并被盖住
→ 改成**两行布局**（第一行台标+名称，第二行日期条横向滚动）。

### 0.4 修复：右侧频道列表 EPG 不实时

根因：`channels.json` 每天 03:30 生成一次，里面的 `now/next` 是**生成那一刻的快照**，
之后一整天都不变 → 列表里永远显示凌晨那一档节目。

修法两层：
1. **服务端**：`/api/channels` 每次请求用 `_refresh_now_next()` 按当前时间重算
   （`programs.json` 按 mtime 缓存，不重复 parse）。
2. **前端**：改为**自适应定时**——算出「最近一个在播节目的结束时刻」，到点 +2 秒再拉，
   换档近乎零延迟；不再固定 30 秒空转。

验收：278 个频道逐一比对，**0 处不一致**。

### 0.5 频道页重新设计

原「直播」+「节目源」两页内容重复（同一份频道列表，一个点了播放、一个点了看详情），
早已在 1.4.0 合并为单页工作台。1.5.0 把左侧菜单项正式定名为 **「直播」**。

### 0.6 文案与布局

- 定时更新页第二栏位 `更新` → **`立即更新`**；按钮移到左侧「上次更新」信息的右边
- `支持回看` → **`支持回看频道`**
- 空白 EPG 区不再抢占剩余空间（`is-empty` 时只占 55px，原来会撑出 456px 死白）

### 0.7 播放器接入页复核（TiviMate → 国内常用）

核查结论（有公开资料支撑）：

| 软件 | 回看占位符 | 能否直接用 |
|---|---|---|
| **酷9** | `${(b)yyyyMMddHHmmss}` | ✅ 用 `fongmi.m3u`；**EPG 解析格式必须手改 XmlTV** |
| **电视直播(mytv-android)** | `${(b)}…` | ✅ 用 `fongmi.m3u` + `epg.xml` |
| 天光云影 | — | ⚠️ **不支持回看**，且项目已停更（3.3.9） |
| DIYP | `{start}` | ✅ 用 `iptv.m3u8` |

酷9 与 FongMi 的回看占位符完全一致 → 共用 `fongmi.m3u`，**不能**给 `kodi.m3u`。
天光云影的导入是「扫码/局域网网页推送」，与 DIYP 完全不是一套流程，故单列标签并注明限制。

### 0.8 图标

换成用户提供的新图（红球+蓝星环+白电视）。按 fnOS 规范：
- `ICON.PNG` 64×64 / `ICON_256.PNG` 256×256，圆角透明（四角 alpha=0）
- **64px 做特调**：轻微放大主体 + 温和 USM 锐化 + 微提对比，抵消直接缩放的发糊
  （纯直缩在 32–48px 会糊成色块）
- 256px 保留原图完整构图与细节

### 0.9 验收

| 轮次 | 范围 | 结果 |
|---|---|---|
| 第一轮 | 接口 / 产物 / 数据质量 / 安全（34 项） | **34/34** |
| 第二轮 | UI / 交互 / 分组 / 文案 / 窗口布局（29 项） | **29/29** |
| 额外 | 启停 3 轮、全新建装、278 频道逐一比对、15 个 4K 台代理可达 | 全通过 |

脚本沉淀在 `verify/round1_api.py`、`verify/round2_ui.js`，可重复执行。

---

## 1. 项目概览

### 1.1 定位

把江苏移动 IPTV（OTT）的直播频道表、回看与节目单，自动转换为通用播放器可直接使用的
M3U / XMLTV / TVBox 配置文件，并以内置 Web 界面提供频道预览与直播试看。

**不依赖任何账号密码**——数据来自移动 OTT 的 `lvps` 接口，按来源 IP 放行。

### 1.2 一句话能力矩阵

| 能力 | 实现 |
|---|---|
| 全量频道表 | 江苏 13 地市聚合去重，实测 278 个频道 / 11 个分组 |
| 节目单 EPG | 前 7 天 ~ 后 3 天，实测约 7 万条 programme |
| 回看（catchup） | 模板化时钟地址，运营商保留约 7 天 |
| 台标本地化 | 抓回 NAS 本地，哈希命名，避免外网掉图 |
| 多播放器格式 | M3U8 / Kodi / FongMi / TVBox / TXT / JSON / 组播 |
| Web 预览 | 频道列表 + 直播试看 + 流规格探测 |
| 定时更新 | 默认每天 03:30，可在应用设置改 |
| 自检与原子发布 | 抽样实测可播 + 隐私扫描，通过才替换 |
| 纯标准库 | 服务端零第三方依赖，Python 3.8+ |

### 1.3 交付物

```
项目根/
├── jsydiptv-1.3.1.fpk        ← 最终安装包（唯一有效版本）
├── fpk/                       ← 开发源码（对应 1.3.1）
├── docs/fnos-ref/             ← fnOS 官方开发文档（更新依据）
├── fpk-安装说明.md             ← 面向使用者的安装/接入说明
├── archive/                   ← 历史版本 fpk（仅供追溯，勿安装）
├── DEVELOPMENT-REPORT.md      ← 本报告
└── VERIFY-CHECKLIST.md        ← 更新后回归验证清单
```

> ⚠️ `archive/` 内的 1.0.0 / 1.1.0 / 1.2.0 / 1.3.0 **均不可用于安装**，
> 各有明确缺陷（见 §8 版本演进）。生产一律用 **1.3.1**。

---

## 2. 架构设计

### 2.1 分层

```
┌──────────────────────────────────────────────┐
│ 前端 fpk/app/www/index.html  单文件 SPA      │
│ 五个页面：直播 / 节目源 / 定时更新 /         │
│           播放器接入 / 软件介绍               │
│ 依赖：hls.min.js（本地内置，非 CDN）          │
└───────────────┬──────────────────────────────┘
                │ HTTP + JSON（同源，iframe 内）
┌───────────────▼──────────────────────────────┐
│ 服务端 fpk/app/server/server.py（1593 行）   │
│  · ThreadingTCPServer  0.0.0.0:18123         │
│  · 路由分发 / API / 流代理 / 定时调度         │
│  · media_probe.py（489 行）流规格解析         │
└───────────────┬──────────────────────────────┘
                │ ① 数据采集（lvps）  ② 媒体代理
┌───────────────▼──────────────────────────────┐
│ 江苏移动 OTT                                            │
│ · EPG  : lvpepg.uni.jsa.bcs.ottcn.com:8080  │
│ · CDN  : 223.110.247.x:6610 / :6410         │
└──────────────────────────────────────────────┘
```

### 2.2 设计取舍（重要，改动前必读）

| 决策 | 原因 |
|---|---|
| **纯标准库，零依赖** | fnOS 应用沙箱无 pip 源，避免装不上；升级不需重装依赖 |
| **HTML 单文件内联 CSS/JS** | 免静态资源 404、免缓存不一致；`hls.min.js` 单独本地文件是唯一例外 |
| **前端不做设置页** | 所有配置走 fnOS「应用设置」原生表单，符合平台规范，避免两套配置打架 |
| **流必须经服务端代理** | 运营商 CDN 不发 `Access-Control-Allow-Origin`，iframe 内直连必失败 |
| **端口在启动参数传入** | `main` 从 `config.json` 读端口再 `--port` 传给服务，单一真源 |

---

## 3. 数据链路（核心）

### 3.1 采集五步流水线

```
[1/5] 拉频道表  →  [2/5] 拉节目单  →  [3/5] 生成输出
     →  [4/5] 自检（抽样可播 + 隐私扫描）  →  [5/5] 原子发布
```

实现位置：`server.py` 的 `run_once()`（第 844 行起）。

### 3.2 接口参数（实测有效，勿随意改）

**接口基地址**
```
EPG_BASE = http://lvpepg.uni.jsa.bcs.ottcn.com:8080/cms-lvp-epg
ASSORT_ID = 115
UA = okhttp/3.12.0
```

**频道表**
```
GET {EPG_BASE}/lvps/channelList
    ?assortId=115&serviceChannelId=&abilityString={URL编码的JSON}
```
`abilityString` 内容（`load_ability()` 生成，紧凑分隔符，整体 URL 编码）：
```json
{"deviceGroupIds":["1697"],"districtCode":"320100"}
```
> `districtCode` 决定额外返回该地市本地台；`all` 模式下遍历 13 个 code 并去重。
> 返回校验：`resultCode` 必须为 `"000"`，否则抛错保留上一版。

**节目单**
```
GET {EPG_BASE}/lvps/getAllProgramlist
    ?startDate=YYYYMMDD&endDate=YYYYMMDD&pos=fullplayer&allDay=true
    &channelType=&relatedId=&copyrightTip=&serviceChannelId=
    &abilityString={...}&uuid={频道uuid}
```
时间窗：`startDate = 今天 - history_days`，`endDate = 今天 + days`。

### 3.3 字段映射

| 源字段 | 用途 |
|---|---|
| `res.url` 中的 `/live/{code}/1.m3u8` | 提取频道 `code` |
| `res.url` 中的 `/(gitv\|cntv\|jsyd)/` | 提取 `cp`（内容提供商标识） |
| `defaultResolution` 匹配 `resolution[].name` | 选定分辨率 → 取该条 url |
| `posterType == 103` | 台标（logo），缺失则取 `poster[0]` |
| `isShowBack` | 是否支持回看（`back` 字段） |
| `multicastUrl` | 组播地址，含 `udp://` 才写入组播源 |
| `uuid` | 全局唯一键，去重与 EPG 关联都靠它 |

### 3.4 地址模板

```python
live_of(ch) = "{cdn}/{cp}/live1/{code}/{code}/1.m3u8"
back_of(ch) = "{cdn}/{cp}/lookback/{code}/{code}?playseek={start}-{end}"
```
`{start}/{end}` 实际是配置的数字，回看时替换为 `YYYYMMDDHHMMSS`。

三种播放器的时间变量语法（缺一不可）：

| 播放器 | start | end | 输出文件 |
|---|---|---|---|
| DIYP / 通用 | `{start}` | `{end}` | `iptv.m3u8`、`直播源.m3u` |
| Kodi | `${start}` | `${end}` | `kodi.m3u`、`直播源_kodi.m3u` |
| FongMi | `${(b)yyyyMMddHHmmss}` | `${(e)yyyyMMddHHmmss}` | `iptv_fongmi.m3u8` |

> ⚠️ 改回看模板时必须同时覆盖这三种，否则某类播放器回看失效。

### 3.5 CDN 调度

```python
HOSTS = [12 个已知 223.110.247.x:6610 地址]   # 内置兜底
SCAN_RANGE = ("223.110.247.", 100, 255)        # --discover 扫描 .100~.255
```
`ordered_hosts()` 优先级：
1. `var/cdn_host.txt` 上次成功的（只取 1 个）
2. 内置 `HOSTS` 12 个
3. `var/hosts.txt`（`--discover` 追加的结果）
4. 以上所有 `:6610` 的 `:6410` 变体

**任一 CDN 必须通过自检才采用**，全失败则保留上一版数据（`RuntimeError`）。

---

## 4. 输出产物规格

### 4.1 精简模式（默认，`output_mode=minimal`）

| 文件 | 说明 |
|---|---|
| `iptv.m3u8` | 主入口，直播 + 回看 |
| `kodi.m3u` | Kodi 专用回看语法 |
| `epg.xml` | XMLTV 节目单 |
| `tvbox.json` | TVBox 一行配置 |
| `channels.json` | 界面索引（含当前/下档节目） |
| `programs.json` | 全量节目（按 uuid 分组） |
| `meta.json` | 更新元信息 |

发布时 `keep` 白名单严格限定上述 7 个文件，其余一律删除。

### 4.2 完整模式（`output_mode=full`）额外产出

`直播源.m3u`、`直播源_kodi.m3u`、`直播源_tvbox_fongmi.m3u`、`直播源.txt`、
`直播源.json`、`组播源.m3u`、`频道清单.csv`、`节目源对照.csv`、
`回看节目单.csv`、`频道对照_机顶盒顺序.csv`、`iptv_fongmi.m3u8`

### 4.3 ASCII 别名（关键兼容性设计）

电视端播放器对 URL 裸中文支持不稳，故提供等价 ASCII 路径：

```
kodi.m3u      → 直播源_kodi.m3u
diyp.m3u      → 直播源.m3u
live.m3u      → 直播源.m3u
fongmi.m3u    → 直播源_tvbox_fongmi.m3u
live.txt      → 直播源.txt
multicast.m3u → 组播源.m3u
```
> 对外宣传一律用 ASCII 地址；中文文件保留便于从共享目录翻找。

### 4.4 EPG 时间窗裁剪

`epg.xml` 只写入 `[今天-5天 00:00, 今天+2天 00:00)` 区间内的 programme。
与采集窗口（前 7 天 ~ 后 3 天）**不同**，这是刻意的：播放器实际读到的条数才是界面该展示的。

---

## 5. API 接口规范

### 5.1 GET（全部同源，无鉴权，仅内网）

| 路径 | 参数 | 作用 |
|---|---|---|
| `/` | — | Web UI |
| `/api/status` | — | 配置、元信息、分组计数、定时状态、服务器时间 |
| `/api/channels` | `group`、`q` | 频道列表（可按分组/关键词过滤） |
| `/api/epg` | `uuid`、`date`(YYYYMMDD) | 某频道节目单 |
| `/api/schedule` | — | 定时任务状态 |
| `/api/catchup` | `uuid`、`start`、`end` | 生成回看地址（含代理地址） |
| `/api/spec` | `u` | 探测流真实规格 |
| `/api/log` | `lines`(默认120) | 服务日志尾巴 |
| `/api/update` | — | 立即更新（同步执行） |
| `/api/discover` | — | 扫描可用 CDN |
| `/logo/{hash}.png` | — | 本地台标 |
| `/stream` | `u` | 直播/回看流代理 |

### 5.2 POST

| 路径 | 体 | 作用 |
|---|---|---|
| `/api/schedule` | `{"enabled":bool,"at":"HH:MM"}` | 改定时（会立即重排调度线程） |
| `/api/update` | — | 立即更新 |
| `/api/config` | 配置 JSON（上限 64KB） | 改配置并重启调度 |

### 5.3 关键约束

**`/stream` 与 `/api/spec` 的主机白名单**（`_is_media_host()`）——防止被当成开放代理：

放行条件（满足其一）：
- 首段 IP 属于 `223.` / `221.` / `112.` / `120.` / `121.`（运营商）
- 内网：`127.*`、`10.*`、`192.168.*`、`172.16~31.*`

> 域名会先 `gethostbyname` 解析再判断；解析不出数字 IP 直接拒绝。
> **新增运营商网段时必须同步改这里**，否则新源播不了。

**回看有效期双重校验**（`/api/catchup`）：
- `end > now + 60s` → 拒绝（节目尚未开始）
- `now - start > 8 天` → 拒绝（运营商只留约 7 天，留 1 天冗余）

**规格缓存**：同一 URL 30 分钟（`_SPEC_TTL=1800`）内复用，上限 300 条，超了淘汰最早的 100 条。

---

## 6. 媒体探测（media_probe.py）

### 6.1 解析流程

```
m3u8 文本 → 取切片列表 → 选倒数第二个切片（最后一个可能仍在写入）
         → 下载该 .ts → 解 PES → 找 SPS/ADTS → 输出规格
```

关键取舍：
- **取倒数第二个切片**：最后一个常写不完整，解析会失败。
- **只有真正的音频 PID 才找音频头**（`0xC0~0xDF`）：若在所有 PID 上盲试，
  视频/无关 PID 的随机字节会伪造出大量假 ADTS 头，票数反压真音频轨（实测如此）。
- 视频按 `0xE0~0xEF`，优先 H.264 SPS（NAL type 7），回退 H.265（（nal[0]>>1）&0x3F == 33）。

### 6.2 输出字段

```json
{
  "bytes": 4105920, "seg_dur": 10.0, "bitrate": 3284736,
  "video": {"w":1920,"h":1080,"codec":"H.264","profile":"Main","level":"4.0","fps":25.0,"sar":"1:1"},
  "audio": {"codec":"AAC","ch":2,"sr":48000}
}
```
> 实测基准值（CCTV-1，1.3.1）：`H.264 1920x1080 | AAC 2ch 48000Hz | 3.3Mbps`

### 6.3 台标明暗判定

`_logo_tone()` 用纯 Python 解 PNG（`zlib` + 反滤波，支持 colorType 0/2/3/4/6），
降采样到最多 64×64，计算 `0.299R+0.587G+0.114B` 亮度均值：

- `≥100` → `light`（内容亮，前端用深底）
- `<100` → `dark`（内容暗，前端用浅底）

只统计 `alpha > 40` 的像素。解析失败回退 `light`。

---

## 7. fnOS 集成（最容易踩坑的部分）

### 7.1 manifest 全字段

```ini
appname=jsydiptv
version=1.3.1
display_name=江苏移动IPTV
source=thirdparty
platform=all                # 纯 Python，无架构二进制
os_min_version=1.1.3100
desktop_uidir=ui
desktop_applaunchname=jsydiptv.main
micro_app=true              # entry 表的真实字段，控制窗口形态
ctl_stop=true               # 显示启动/停止
service_port=18123
checkport=true              # 启动前检查端口占用
```

### 7.2 桌面入口（`app/ui/config`）

```json
{
  ".url": {
    "jsydiptv.main": {
      "title": "江苏移动IPTV",
      "icon": "images/icon_{0}.png",
      "type": "iframe",
      "protocol": "http",
      "port": "${wizard_port}",
      "url": "/",
      "allUsers": true
    }
  }
}
```

> 🔴 **`${wizard_port}` 是 1.3.0 空白页事故的根本原因**：
> 该变量由 **`wizard/install`** 提供。包内若只有 `wizard/config`（应用设置），
> 安装时变量取不到值，**变量名会被原样写进数据库**：
> ```sql
> -- trim_sac 库 entry 表
> {"protocol":"http","port":"${wizard_port}","path":"/"}   ← 桌面窗口连不上 → 一片空白
> ```
> 诊断标志：`entry.url` 里出现字面 `${...}`。
> 实测校验：不带 `--env` 装包会报 `Something wrong with environment variables.`，
> 说明系统确实在校验向导字段。

### 7.3 生命周期脚本

| 脚本 | 职责 | 关键点 |
|---|---|---|
| `install_init` | 检查 python3 ≥ 3.8 | 失败写 `TRIM_TEMP_LOGFILE` |
| `install_callback` | 首次生成 `config.json` | **用 `SELF_DIR` 定位同目录脚本**，不能靠 `$TRIM_APPDEST/../cmd` |
| `main` | start/stop/status | 见 §7.5 的进程管理 |
| `config_init` | 表单预校验（端口占用预检） | 本应用自己占用则放行 |
| `config_callback` | 写 `config.json` 并重启服务 | 校验失败即拒绝 |
| `upgrade_*` / `uninstall_*` | 停服务、清残留 | 保留 `var/data`（用户可能已引用） |

**退出码约定**：`0` 成功/运行中，`1` 失败，`3` 未运行（status 专用）。

### 7.4 环境变量（唯一真源，禁止硬编码路径）

| 变量 | 指向 |
|---|---|
| `TRIM_APPDEST` | `target` 根（`server/`、`www/`、`ui/` 在此下） |
| `TRIM_PKGETC` | 配置目录（`config.json`） |
| `TRIM_PKGVAR` | 数据目录（`data/`、日志、pid） |
| `TRIM_TEMP_LOGFILE` | 面向用户的错误输出 |
| `TRIM_SERVICE_PORT` | manifest 声明的端口 |

> 🔴 **装在哪由用户选存储空间决定**。实测落在 `/vol4/@appcenter/jsydiptv`，
> 而 `/var/apps/jsydiptv/target` 是软链接。
> **脚本里绝不能出现 `/vol1`、`/vol4` 等硬编码**，一律用 `TRIM_*`。

### 7.5 进程管理（1.3.1 的核心修复）

旧实现只按 PID 文件停进程，一旦出现孤儿（PID 文件丢失/多实例/卸载删了 var），
进程会残留占用端口，导致下次启用报「端口被占用」。

**现行实现：扫 `/proc` 按本应用独有路径匹配**

```bash
own_pids(){
    for d in /proc/[0-9]*; do
        [ -r "$d/cmdline" ] || continue
        c="$(tr '\0' ' ' < "$d/cmdline")" || continue
        case "$c" in *"$SRV"*) echo "${d#/proc/}" ;; esac
    done
}
```
`$SRV = $TRIM_APPDEST/server/server.py`，路径唯一，**不会误杀其他 python 进程**。

配套三条规则：
1. `start` 前若端口被**本应用**残留进程占 → 先 `kill_own` 再起（自动愈合）
2. 端口被**别的程序**占 → 明确报错提示换端口，不静默失败
3. `stop`：TERM 等 10 秒，未退则 KILL，覆盖所有实例

### 7.6 权限模型

```json
// config/privilege
{"defaults":{"run-as":"package"},"username":"jsydiptv","groupname":"jsydiptv"}

// config/resource
{"data-share":{"shares":[{"name":"jsydiptv"}]}}
```
以专用用户 `jsydiptv` 运行（非 root），并通过 `shares` 暴露一个数据共享目录。

---

## 8. 版本演进与踩坑档案

| 版本 | 状态 | 缺陷 |
|---|---|---|
| 1.0.0 | 曾可用 | 界面为旧三栏布局；`entry` 端口写死 `18123` |
| 1.1.0 | **包结构错误** | 包内多一层 `jsydiptv/` 根目录（fnpack 旧格式），装不上 |
| 1.2.0 | 空白页 | 引入 `${wizard_port}` 但**缺 `wizard/install`** → 变量未替换 |
| 1.3.0 | 装不上 | 手工打包时 manifest 版本未改（sed 没匹配上）+ 图标不在 tar 根层 → `code 10111` |
| **1.3.1** | ✅ **当前** | 无（三处修复齐备） |

### 8.1 空白页事故链（1.2.0）

表面看服务全正常：端口在听、`curl` 返回 200、日志 `success`。
**唯一证据在数据库**：
```sql
SELECT app_name,url FROM entry WHERE app_name='jsydiptv';
-- {"protocol":"http","port":"${wizard_port}","path":"/"}   ← 变量名没被替换
```
同时 `app_env` 表里该应用**安装变量 0 条**（对照 watchcow 有记录）。
全系统只有这一条的端口是未替换的变量名。

排查中被排除的方向（都实测过，避免以后重复走弯路）：
`${wizard_port}` 语法（官方 V1.1.8+ 支持）／系统版本（1.2.1102 够新）／
静态资源 404（`/`、`/hls.min.js` 均 200）／只绑 localhost（实为 `0.0.0.0`）／
首次拉数据阻塞（实测仅 13 秒，且 `do_update` 有 try/except 不崩）。

### 8.2 端口被占事故链（1.3.0 遗留）

`appcenter-cli stop` / `uninstall` 都报 `success`，但进程仍活（`PPID=1` 孤儿），
占着 18123 → 再启用被 `checkport=true` 拦。根因即 §7.5 的 PID 文件盲区。

### 8.3 手工打包四坑（`fnpack` 不可用时）

1. **`ICON.PNG` / `ICON_256.PNG` 必须在 tar 根层级**，否则
   `checkPackage iconPath .../ICON.PNG is not exist`（code 10111）
2. **版本号必须真的改**：`sed` 没匹配会静默沿用旧值，系统按旧版本名找解压目录而失败
3. 卸载后 `/vol4/@apphome|@appmeta|@appshare/jsydiptv` 会留空壳，**干扰重装**，需手工清
4. 权限：`ICON*/manifest/config/*/wizard/*` = 644，`cmd/*` = 755（图标 700 曾是失败诱因）

---

## 9. 配置项参考

| 配置键 | 默认 | 范围/取值 | 说明 |
|---|---|---|---|
| `port` | 18123 | 1024–65535 | 服务端口 |
| `district` | `all` | `all` 或 13 地市 code | 决定本地台范围 |
| `update_at` | `03:30` | `HH:MM` | 每日更新时间 |
| `schedule_enabled` | `true` | bool | 定时开关 |
| `epg_enable` | `true` | bool | 是否拉节目单 |
| `days` | 3 | 0–30 | 往后抓几天 |
| `history_days` | 7 | 0–30 | 往前抓几天（回看） |
| `backup_days` | 7 | 0–30 | 写入 m3u 的 `catchup-days` |
| `localize_logo` | `true` | bool | 台标本地化 |
| `output_mode` | `minimal` | `minimal`/`full` | 输出文件集 |

**13 个地市 code**：
`320100`南京 `320200`无锡 `320300`徐州 `320400`常州 `320500`苏州 `320600`南通
`320700`连云港 `320800`淮安 `320900`盐城 `321000`扬州 `321100`镇江
`321200`泰州 `321300`宿迁

---

## 10. 可靠性设计

### 10.1 原子发布

写入 `var/data/.build/` → 自检通过 → `publish()` 逐个 `os.replace` 到正式目录
→ 按 `keep` 白名单删除多余文件。**任何一步失败都不动正式目录**。

### 10.2 抽样自检（`validate()`）

- 每个分组取 4 个 + 随机 10 个（`Random(2026)` 固定种子，结果可复现）
- 并发 10 实测可播数
- **通过条件**：`live_ok >= max(3, 样本数×0.8)` 且无隐私泄漏
- 隐私正则（命中即拒绝发布）：
  ```
  手机号      1[3-9]\d{9}
  MAC        ([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}
  STBID      00411300\d{10}
  令牌字段    OTTUserToken|IASHttpSessionId|accountinfo|UserName
  ```
  扫描所有 `.m3u/.m3u8/.txt/.json/.csv/.md/.xml`

### 10.3 并发与超时参数

| 场景 | 参数 |
|---|---|
| `http_get` | timeout 25s，重试 2 次，间隔 0.5s |
| `http_get_bytes`（台标） | timeout 8s，重试 1 次，间隔 0.2s |
| 频道表/节目单 | 节目单并发 8 |
| 台标本地化 | 并发 12，文件 ≥200 字节才算成功 |
| 自检探测 | 并发 10 |
| CDN 扫描 | 并发 24，timeout 3s，不重试 |
| 流代理 `_fetch` | timeout 12s |
| 规格探测 | timeout 10s，缓存 30 分钟 |

### 10.4 定时调度

`_schedule_loop` 用 `threading.Event.wait(gap)` 而非 `sleep`——
配置变更可**立刻唤醒**重排，不用等旧周期走完。
计算目标时间时若已过点，自动 +1 天。

---

## 11. 安全边界

| 项 | 措施 |
|---|---|
| 开放代理风险 | `/stream`、`/api/spec` 主机白名单（§5.3） |
| 路径穿越 | 只从 `WWW`/`DATA` 提供文件，`/logo/` 限定哈希命名 |
| 隐私泄漏 | 发布前四类正则扫描，命中即拒绝 |
| 凭据 | **不含任何账号/密码/令牌/MAC/STBID** |
| 运行身份 | 专用用户 `jsydiptv`，非 root |
| 异常兜底 | `do_GET` 统一 try/except，返回 JSON 错误而非断连 |

> 前提：**监听 0.0.0.0，仅应在内网使用**。若需外网访问，须自行加反代与鉴权。

---

## 11.5 1.4.0 版本变更与三缺陷根因（2026-11-24）

### 11.5.1 需求实现总览

| 需求 | 实现 |
|---|---|
| FongMi + TVBox 双兼容 | 新增 `fongmi.json`/`fongmi.m3u`/`epg/<uuid>/<date>.json`，与 TVBox 三件套并存；播放器接入页新增 FongMi 标签并默认选中 |
| 修 EPG 叠放 | 去掉 `scrollIntoView`，改 `#pl` 独立滚动 + 头部固定 |
| 修直播黑屏 | 加起播守护（12s 无画面即提示）+ 显式处理 `play()` 拒绝 + 有声优先/静音重试 + 「外部打开」按钮 |
| 修台标纯黑 | 台标改存**相对路径**，M3U/EPG 用 `logo_of()` 转绝对地址 |
| 重设频道页 | 「直播」+「节目源」合并为单页工作台（左播放器/右列表） |

### 11.5.2 三个缺陷的根因（实测确认，非推测）

**① 台标纯黑** —— 图标本身没问题
```
实测：278 个 PNG 全在 data/logo/，真实 200×68 RGBA，
     亮度 light=270 / dark=8，全透明 0，解析失败 0
```
真正原因：`localize_logos()` 把 logo 写成**硬编码绝对地址**
`http://192.168.1.100:18123/logo/xxx.png`。一旦用域名/别的 IP/fnOS 桌面
iframe 访问，`<img>` 加载失败 → 露出 `.lg.t-light` 的深色底 `rgb(22,25,29)`
→ 视觉上就是「纯黑方块」。

修复：内存里存相对路径 `/logo/xxx.png`（浏览器按当前源解析，任何访问方式都通）；
写进 m3u/epg.xml 时由 `logo_of()` 转绝对地址（电视端播放器必须有完整 URL）。

> ⚠️ 这是双重需求，改任一处都要同时考虑两边：界面要相对、播放器要绝对。

**② 直播黑屏** —— 静默失败的连锁
```
<video id="vid" controls playsinline style="display:none">   ← 无 muted
v.play().catch(function(){})                                 ← 失败被吞掉
```
自动播放策略拒绝有声视频的 `play()` 时，video 停在
`background:#000` 上，代码又不报错 → 用户只看到一片纯黑、无任何提示。

修复三件套：
1. `_tryPlay()`：先试**有声**（点频道是用户手势，通常允许），被拒再静音重试并提示
2. `_startWatchdog()`：12 秒内 `readyState<3` 或 `videoWidth===0` 就明确告知「连上流但没画面」
3. 新增「外部打开」按钮，把代理地址交给系统播放器

**③ EPG 叠放** —— 滚错了容器
```
showDetail 里 curEl.scrollIntoView({block:'center'})
```
滚的是外层 `#srcDet`（overflow-y:auto），不是 `#pl`。
结果头部（台标/正在播出/下一档/按钮）被滚出视野，节目单贴到面板最上方，
看起来就像「EPG 叠在头部上面」。
```
实测：#pl top=-7383、height=10700；det scrollHeight=11094 vs clientHeight=501
```
修复：`.plist` 设 `max-height:46vh;overflow-y:auto` 形成独立滚动区，
滚动只发生在 `#pl` 内部（`pl.scrollTop = ...`），绝不 `scrollIntoView`。

### 11.5.3 1.4.0 新增的技术参数

```
FongMi 配置     /fongmi.json   （sites 空，不做点播）
FongMi 直播清单 /fongmi.m3u    （catchup="default"，回看 ${(b)}/${(e)}）
FongMi 节目单   /epg/<uuid>/<YYYY-MM-DD>.json
                格式 {"date":"...","epg_data":[{"start":"HH:mm","end":"HH:mm","title":"..."}]}
                只生成「今天」；查不到返回空表而非 404（否则面板一直转圈）
TVBox/通用      /tvbox.json + /iptv.m3u8 + /epg.xml
Kodi            /kodi.m3u    （回看 ${start}/${end}）
```

三种回看语法（改动必须同步三处）：
| 文件 | catchup | 回看占位符 |
|---|---|---|
| `iptv.m3u8` | `append` | `{start}` / `{end}` |
| `kodi.m3u` | `append` | `${start}` / `${end}` |
| `fongmi.m3u` | `default` | `${(b)yyyyMMddHHmmss}` / `${(e)yyyyMMddHHmmss}` |

精简模式发布白名单（10 项，含目录）：
```
iptv.m3u8 kodi.m3u fongmi.m3u fongmi.json epg.xml tvbox.json
meta.json channels.json programs.json epg/
```
> ⚠️ `epg` 必须在白名单里，否则发布时整目录被清空（1.4.0 首次实测踩到）。
> `publish()` 对 `epg/` 是**整体替换**，避免过期频道的旧节目单残留。

### 11.5.4 界面尺寸关键值（台标模糊的教训）

运营商台标多为 **200×68（≈2.94:1 宽屏）**。
原图标框 44×30 会把图压到 **38×12.9（仅原图 19%）**，文字必然发虚。
1.4.0 调整为 **58×36**，渲染 **54×18.4（27%）**，明显清晰。
> 改图标框尺寸时，务必按原图比例验算，别只看框大不大。

## 12. 已知限制

1. **必须在江苏移动网络下**才能播放（CDN 按来源 IP 放行）。
   非移动网络：节目单可生成，直播不通。
2. 回看约 7 天，超出由运营商侧失效（`/api/catchup` 已主动拦截并给出明确文案）。
3. CDN 有并发限制，不宜大规模分发。
4. **改端口后 `entry` 不会自动同步**：服务会切到新端口，但桌面入口仍指旧端口，
   图标打不开。→ **建议端口保持 18123**；真需改动则重装一次让入口重新注册。
5. 前端试看依赖浏览器 MSE 的 H.264 + AAC 能力；缺能力时提示改用电视端播放器。

---

## 13. 后续更新必查清单

更新任何一版前后，逐项核对（详细操作见 [验收清单](../verification.md)）：

### 13.1 打包前（源码层）

- [ ] `manifest` 的 `version` 已改，且 `grep ^version manifest` 复核
- [ ] `wizard/install` 存在，且含 `wizard_port` 字段
- [ ] `app/ui/config` 的 `port` 与 `manifest.service_port` 语义一致
- [ ] `cmd/main` 用 `bash -n` 语法检查通过
- [ ] 服务端 `python3 -m py_compile` 通过
- [ ] 源码内**无硬编码卷路径**（`grep -E '/vol[0-9]'` 应为空）
- [ ] `_is_media_host()` 覆盖新用到的网段

### 13.2 打包后（包结构层）

- [ ] `ICON.PNG`、`ICON_256.PNG`、`manifest`、`app.tgz` 在 **tar 根层级**
- [ ] `wizard/` 内**同时**有 `install` 和 `config`
- [ ] 包内 `app.tgz` 含 `server/media_probe.py`、`www/hls.min.js`
- [ ] 包内文件与源码逐字节一致（`diff -r`）

### 13.3 安装后（运行时）

- [ ] `appcenter-cli status` = `running`
- [ ] `entry.url` 的 `port` 是**真实数字**，不是 `${...}`
- [ ] 首页 `200`，`<title>江苏移动 IPTV</title>`
- [ ] `/api/status` 返回频道数正常
- [ ] `/api/spec` 能返回规格
- [ ] `stop` 后端口**真正释放**、进程全无
- [ ] 再 `start` 能正常运行（反复启停 3 次）
- [ ] 卸载后无残留进程与端口占用

### 13.4 数据层

- [ ] `channels.json` 频道数与 `entry` 分组一致
- [ ] `epg.xml` 的 programme 条数与界面展示一致
- [ ] 抽样 3 个频道：直播地址 200、回看地址可播

---

## 14. 快速定位手册

| 症状 | 首选检查 |
|---|---|
| 桌面图标点开一片空白 | `SELECT url FROM entry WHERE app_name='jsydiptv'` 看有无 `${` |
| 启用报端口被占用 | `ss -lntp \| grep 18123` → 查 `/proc/<pid>/cmdline` 是不是自己的孤儿 |
| 页面 200 但无频道数据 | `var/data/channels.json` 是否存在；看 `/api/log` |
| 直播播不了（数据正常） | 是否在江苏移动网络；`/api/spec` 是否 403（白名单） |
| 回看失效 | 是否超 7 天；当前频道 `back` 是否为 1 |
| 台标不显示 | `var/data/logo/` 下哈希文件数；共享目录可访问性 |
| 更新一直失败 | `/api/log` 看 `[4/5] 自检` 的 `live` 与 `privacy` |
| 装不上报 10111 | 图标是否在 tar 根层；manifest 版本是否真改 |

**关键日志位置**：`$TRIM_PKGVAR/service.log`（服务）、`install.log`（安装）、
`/var/log/trim_app_center/info.log`（平台侧）。

---

## 15. 关键技术参数速查表

```
应用标识        jsydiptv                          版本 1.3.1
服务端口        18123（默认，0.0.0.0 监听）
Python          3.8+（纯标准库，零依赖）
EPG 接口        http://lvpepg.uni.jsa.bcs.ottcn.com:8080/cms-lvp-epg
assortId        115
deviceGroupIds  1697
UA              okhttp/3.12.0
CDN 端口        6610（主）/ 6410（备）
CDN 扫描段      223.110.247.100~255
实测频道数      278（全江苏 11 分组）
EPG 条目        约 7 万条 programme
规格缓存        1800 秒，上限 300 条
回看保留        约 7 天（代码拦截阈值 8 天）
自检通过线      ≥max(3, 样本×0.8) 且隐私干净
定时默认        03:30（Event.wait 可即时唤醒）
流代理超时      12s     规格探测 10s     频道表 25s/重试2
并发            节目单8 台标12 自检10 扫描24
数据目录        $TRIM_PKGVAR/data（logo/ 子目录）
发布白名单      7 个文件（minimal 模式）
```

---

## 16. 变更影响提示（改代码前必读）

| 想改的东西 | 连带影响 |
|---|---|
| 回看模板 | 必须同步 3 种播放器语法（§3.4） |
| `_is_media_host()` | 影响 `/stream` 与 `/api/spec`，漏改新网段会播不了 |
| 输出文件名 | 必同步 `ALIASES`、`keep` 白名单、`tvbox.json`、前端「播放器接入」页 |
| `media_probe` 解析逻辑 | 直接影响界面规格显示；音频必须限定 `0xC0~0xDF` |
| `manifest.service_port` | 与 `app/ui/config` 的 `port` 语义一致，且 `config_callback` 默认值同步 |
| 新增配置项 | 要同时改：`wizard/config`、`wizard/install`(若安装期需要)、`config_callback`、`DEFAULTS` |
| 数据目录结构 | 影响 `keep` 白名单、`publish()`、共享目录权限 |

---

*本报告基于 1.3.1 源码逐行核实生成，所有参数均为实际取值而非估算。
更新版本后请同步修订本文件与 [验收清单](../verification.md)。*