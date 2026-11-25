# 上游两包对比分析

感谢恩山大佬lff520520提供思路和实现方案，原贴地址：https://www.right.com.cn/forum/thread-8499027-1-2.html

> 分析时间：2026-11-24
> 包 A：`web_7bcb3d91.zip`（下文简称 **A**）
> 包 B：`web_e2a50ea8.zip`（下文简称 **B**，用户说明为最新发布）
> 归档位置：`upstream/7bcb3d91-fongmi-v4/`、`upstream/e2a50ea8-tvbox/`
>
> 📌 另有 `web_4375c759.zip`、`web_5166ae7b.zip` 两个文件，经 MD5 校验与本包 B **完全相同**
> （`100c837861258ba54f220a70d4711257`，内容逐字节一致），属重复下载，已清理。
> 所以实际只有**两个不同版本**需要对比。

---

## 0. 结论先行

**这两个包不是同一产品的新旧版本，而是两条不同产品线。**

| | A（7bcb3d91） | B（e2a50ea8） |
|---|---|---|
| 自我定位 | **FongMi TV 专版 v4** | **通用归档（TVBox 系）** |
| 面向客户端 | 只服务 FongMi TV 5.9 | TVBox / TiviMate / DIYP / Kodi |
| 产物形态 | `fongmi.json` + `fongmi.m3u` + `epg/<id>/<日期>.json` | `iptv.m3u8` + `epg.xml` + `tvbox.json`（+完整模式） |
| 分组来源 | 机顶盒**原版分组接口**（11 组，一台可属多组） | **规则推断**（央视/卫视/江苏/地方/其他，单组） |
| 节目单 | 只做**当天**，按频道拆 JSON | XMLTV，前 5 天 ~ 明天 |
| 一次打包带产物 | 否（只有 4 个脚本） | **是**（含 278 频道 m3u + 50941 条节目） |

### ⚠️ 与“带 2 最新”的说法有一处冲突，需你确认

zip 内部文件时间戳：

```
A（7bcb3d91）  updater.py  2026-11-24 10:08   autostart 11-24 10:37   start 11-24 10:38
B（e2a50ea8）  updater.py  2026-11-23 14:32   autostart 11-23 15:23   start 11-23 13:44
```

**按时间戳，A 比 B 新一天多。** 但 A 只有裸脚本、B 带完整运行产物（更像“已完成发布”的快照）。
两种解读：

1. **A 是后出的新分支**（作者做完 B 后又单独做了 FongMi 专版）→ 那 A 才是最新
2. B 是正式发布、A 是同期试验分支 → 符合你的说法

A 的 docstring 自称 `v4（2026-11-24）`，说明它确实是个**独立迭代的产品线**，不是 B 的旧版。
**建议你确认：到底是要“FongMi 专版”还是“通用 TVBox 版”？** 这决定后续该基于哪条线做。

---

## 1. 文件级差异总览

```
共同文件：autostart.sh / start.sh / stop.sh / updater.py
A 独有：无（本体只有 4 个文件）
B 独有：www/epg.xml  www/iptv.m3u8  www/tvbox.json
```

| 文件 | A | B | 说明 |
|---|---|---|---|
| `stop.sh` | 370B | 370B | **完全相同** |
| `start.sh` | 3339B | 3320B | 几乎相同，仅文案与模式说明不同 |
| `autostart.sh` | 12966B | 10654B | A 更全（多了产物逐项验活、动态列地址） |
| `updater.py` | 792 行 | 921 行 | 差异最大：**+663 / −524 行** |

---

## 2. updater.py 架构差异

### 2.1 函数命名对照（判断血统的关键）

| A 的命名 | B 的命名 | 备注 |
|---|---|---|
| `get()` / `get_bytes()` | `http_get()` / `http_get_bytes()` | 同功能改名 |
| `localize()` | `localize_logos()` | 同功能改名 |
| `probe()` | `probe_live()` | 同功能改名 |
| `collect(home_city)` | `collect(districts, days, hist_days)` | **参数模型不同** |
| `channel_list(ab, assort)` | `build_channels(ability, district)` | |
| `programs(chs, days, hist)` | `build_programs(channels, ability, hist_days, days)` | |
| `groups(ab)` | — | **A 独有**：调原版分组接口 |
| `city_group_name()` / `group_order()` / `entry_key()` | — | **A 独有**：多组归属体系 |
| — | `gkey()` / `ordered_hosts()` / `load_ability()` | B 独有 |
| `write_outputs(out, cdn, chs, progs, stamp, base_url, home_city)` | `write_outputs(out, cdn, channels, programs, backup_days, days, base_url, minimal)` | **签名不同** |

> **重要发现**：B 的函数命名（`http_get` / `build_channels` / `collect(districts,...)` /
> `publish(build,out,keep)` / `validate(out,channels,minimal)`）与我们项目的 `fpk` **完全同源**，
> 可以判断 B 就是 `jsydiptv` fpk 的前身/同门分支。

### 2.2 A 的核心设计：以机顶盒为准

```python
PROVINCE   = "320000"   # 全省
HOME_CITY  = "320600"   # 机顶盒所在地：南通
ASSORT_ALL = "115"      # 机顶盒"全部"
ASSORT_LOCAL = "120"    # 机顶盒"本地"

GROUP_ASSORTS = {208:"超高清", 116:"央视", 120:"本地", 121:"卫视", 170:"体育",
                 211:"专业频道", 169:"影视", 171:"少儿", 172:"生活",
                 122:"广播", 173:"购物"}   # 机顶盒电视墙原版 11 组
```

**A 用了两个 B 和 fpk 都没有的接口**：

```
GET {EPG_API}/lvps/getOpenNewAssortment?serviceChannelId=&abilityString={ab}
    → content.assortList  内含 assortId / assortName / sortNum
```

这带来一个**结构性优势**：

- **一台可属多组**：`entries = [(ch,g) for ch in chs for g in ch["groups"]]`
  （例如「江苏卫视」同时出现在「本地」和「卫视」组，和机顶盒电视墙一致）
- 分组名是运营商原版（超高清/专业频道/广播/购物），而非自定义推断
- 13 地市「本地」清单用**交集剔除**省级台：

```python
common = set.intersection(*[{c["uuid"] for c in v} for v in lists.values()])
# 出现在所有 13 个地市 = 省级台，不归属任何单市
```

> 相比之下 B（和我们的 fpk）是**规则推断**分组：
> `CCTV开头→央视`、`含"卫视"→卫视`、`num以9开头→地方`、`含"江苏"→江苏`、`其余→其他`。
> 单组归属，无法表达“一台多组”，分组名也不是运营商原版。

### 2.3 B 的设计：全地市聚合 + 多格式输出

```python
ASSORT_ID = "115"
ABILITY_BASE = {"deviceGroupIds": ["1697"]}
GROUP_ORDER = ("央视", "卫视", "江苏", "地方", "其他")
STYLES = (("diyp",   "直播源.m3u",              "{start}",              "{end}"),
          ("kodi",   "直播源_kodi.m3u",         "${start}",             "${end}"),
          ("fongmi", "直播源_tvbox_fongmi.m3u", "${(b)yyyyMMddHHmmss}", "${(e)yyyyMMddHHmmss}"))
```

B 遍历 13 个 `districtCode` 各拉一次频道表，`uuid` 去重合并，得到全省 278 台。

---

## 3. 产物形态对比（最影响使用的差异）

### 3.1 A —— FongMi 专用三件套

```
www/
├── fongmi.json              # 配置地址（lives + epg 模板）
├── fongmi.m3u               # 直播清单
├── epg/<频道uuid>/<YYYY-MM-DD>.json   # 按天节目单（每频道一个文件）
└── logo/<md5>.png           # 台标
```

`fongmi.json` 结构：
```json
{
  "notice": "...FongMi 5.9 专版...",
  "sites": [],                       // 空，不做点播
  "lives": [{
    "group": "江苏移动IPTV", "name": "机顶盒原版", "type": 0,
    "url": "{base}/fongmi.m3u", "playerType": 1,
    "ua": "okhttp/3.12.0",
    "epg": "{base}/epg/{id}/{date}.json"
  }],
  "epg": ["{base}/epg/{id}/{date}.json"]
}
```

按天 JSON 格式（FongMi 5.9 唯一认的接口）：
```json
{"date":"2026-11-24","epg_data":[{"start":"20:00","end":"21:00","title":"..."}]}
```
> 只生成**当天**；±1 天返回空表。跨零点由客户端自己 +1 天。

**代价**：文件数量 = 频道数（278 个 JSON），且历史节目单客户端拉不到。
**收益**：FongMi 面板渲染完全正确，不必依赖 XMLTV 解析。

### 3.2 B —— 通用三件套（+完整模式）

```
www/
├── iptv.m3u8      # 直播 + 回看（ASCII 名，通用）
├── epg.xml        # XMLTV，前 5 天 ~ 明天
└── tvbox.json     # TVBox 一行配置
```

实测产物（包内自带，2026-11-23 13:00 生成）：
- `iptv.m3u8`：**278 频道**，118,872 字节
- `epg.xml`：**50,941 条 programme**，7,885,593 字节
- `tvbox.json`：`{"lives":[{"url":"http://10.0.0.2:12123/iptv.m3u8"}],"epg":[...]}`

**收益**：一个文件通吃所有播放器，节目单是标准 XMLTV。
**代价**：FongMi 5.9 对它自己的按天 JSON 接口支持更好，走 `epg.xml` 可能不如原生格式。

---

## 4. 回看地址实现差异（实测已验证）

| | 生成用的地址 | 自检用的地址 |
|---|---|---|
| A | `.../lookback/{cp}/{code}/{code}/index.m3u8?playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}` | `.../lookback/{cp}/{code}/{code}?playseek={Y}200000-{Y}210000` |
| B | `.../lookback/{cp}/{code}/{code}?playseek={start}-{end}` | 同生成 |
| 本 fpk | 同 B | 同 B |

### 实测结论（NAS 上实跑）

```
A 格式（带 index.m3u8）：HTTP 302 → 最终 200，#EXTM3U，121391 字节 ✅
B 格式（不带 index）：  HTTP 302 ×2 → 最终 200，#EXTM3U，121391 字节 ✅
```

**两种写法最终都可用**——B 的写法由运营商服务端自动 302 两次补上 `index.m3u8`。
所以这**不是缺陷**，只是 A 显式写了、B 依赖重定向。

> 但 A 存在**内部不一致**：`write_outputs` 生成带 `index.m3u8`，
> 而 `validate` 自检用不带 `index.m3u8` 的格式。虽然两者都通，
> 但这意味着**自检没有验证真正下发的那个 URL**，属于测试盲区，值得指出。

---

## 5. autostart.sh 差异

两者进程管理逻辑**完全相同**（同一作者脉络）：

```sh
some_pids()   # 找手工拉起的 updater.py
port_pids()   # ss -lntp 找占端口进程
# 端口被"旧 updater.py"占 → 自动释放；被其它程序占 → 提示换端口
```

差别在于**验活项**：

| 验活项 | A | B |
|---|---|---|
| 状态 active / 进程数=1 | ✅ | ✅ |
| 端口监听 | ✅ | ✅ |
| 主产物 200 | `fongmi.json` | `tvbox.json` |
| **地址一致性校验** | ✅ `grep BASE_URL fongmi.json` | ❌ 无 |
| **产物逐项列表+字节数** | ✅ 遍历 www 实有文件 | 部分 |
| 按天 JSON EPG 抽查 | ✅ 抽 1 台今天，验 `epg_data` | — |
| `.meta.json` 展示生成摘要 | ✅ | 有文件但不展示 |

**A 的 autostart 更完整**：它会在服务就绪后动态列出“www 里真实存在的产物”并逐个 curl 验 200，
而不是写死文案。这个做法值得借鉴到我们的 fpk。

---

## 6. 与我们项目（jsydiptv fpk v1.3.1）的关系

| 维度 | 本 fpk 1.3.1 | A（FongMi v4） | B（TVBox） |
|---|---|---|---|
| 分发形式 | **fnOS fpk 原生应用** | 裸脚本 + systemd | 裸脚本 + systemd |
| 安装方式 | 应用中心手动安装 | `sh autostart.sh install`（需 root） | 同 A |
| 配置入口 | fnOS「应用设置」原生表单 | 改脚本变量 / 命令行参数 | 同 A |
| 桌面入口 | ✅ 桌面图标 iframe | ❌ 无（只给 URL） | ❌ 无 |
| 分组来源 | 规则推断 | **原版接口（多组）** | 规则推断 |
| 节目单 | XMLTV + 程序内索引 | 按天 JSON | XMLTV |
| 输出模式 | 精简 / 完整 | 固定 FongMi | 精简 / 完整 |
| 流规格探测 | ✅（H.264/HEVC SPS + ADTS/AC3） | ❌ | ❌ |
| CDN 自动发现 | ✅ `--discover` | ✅ | ✅ |
| 原子发布 | ✅ | ✅ | ✅ |
| 隐私扫描 | ✅ 4 类 | ✅ 5 类 | ✅ 5 类 |

### 6.1 B 与 fpk 的同源证据

B 的以下函数签名与 fpk **逐字相同**：

```python
def http_get(url, timeout=25, retries=2)
def http_get_bytes(url, timeout=8, retries=1)
def build_channels(ability, district="", quiet=False)
def build_programs(channels, ability, hist_days, days, quiet=False)
def collect(districts, days, hist_days=7)
def gkey(ch)
def write_outputs(out, cdn, channels, programs, backup_days, days, base_url="", minimal=False)
def probe_live(url, timeout=8)
def validate(out, channels, minimal=False)
def publish(build, out, keep=None)
def run_once(out, days, backup_days, districts, hist_days=7, base_url="", minimal=False)
```

→ **B 是 fpk 的上游源头**，fpk 在此基础上加了 fnOS 应用封装（manifest/wizard/cmd/ui）、
Web 预览界面、流规格探测、媒体代理等。

### 6.2 A 里值得吸收的东西

1. **原版分组接口 `getOpenNewAssortment`**
   能让分组名与机顶盒电视墙一致（超高清/专业频道/广播/购物），
   且支持“一台多组”。这是当前 fpk 的明显短板。

2. **13 地市本地台的交集剔除算法**
   ```python
   common = set.intersection(*[{c["uuid"] for c in v} for v in lists.values()])
   ```
   自动识别“出现在所有地市 = 省级台”，比按名字猜更可靠。

3. **autostart 的动态产物验活**
   按 www 实际存在文件逐个验 200，而非写死文件名。可借鉴到 fpk 的 `main` 自检。

---

## 7. 需要注意的问题

### 7.1 A 的自检盲区
生成带 `index.m3u8`、自检不带 → 自检没验证真实下发 URL（详见 §4）。

### 7.2 A 的产物数量
FongMi 按天 JSON 是**每频道一个文件**。278 频道 = 278 个 JSON，
且只保留当天。若你的播放器不是 FongMi，这套产物几乎没用。

### 7.3 B 带的 `www/` 是运行快照
`tvbox.json` 里写死 `http://10.0.0.2:12123`（作者自己的测试地址）。
**直接部署会把地址写错**，必须重新跑一次 updater 让它按实际 IP 生成。

### 7.4 两者都是 systemd 直装，与 fpk 路线冲突
`autostart.sh` 会写 `/etc/systemd/system/iptv-updater.service`，
和 fnOS 应用中心的托管机制是两套东西。**不要与已装的 fpk 同时运行**，会抢端口（都默认倾向 12123/18123）。

### 7.5 端口默认值不同
A/B 的 `start.sh` / `autostart.sh` 默认 **12123**，我们 fpk 默认 **18123**。
两者若共存，端口不冲突，但会造成“到底连哪个”的混乱。

---

## 8. 建议

**先明确你要哪个产品形态**，再决定动作：

### 情形一：要 FongMi 专版（A）
→ A 就是最新，但它没有 fnOS 应用封装（无桌面图标、无应用设置表单）。
可选做法：把 A 的**分组接口**和**本地台交集算法**移植进我们的 fpk，
产品形态仍是 fpk（有桌面入口），但分组质量对齐机顶盒原版。

### 情形二：要通用 TVBox 版（B）
→ B 是 fpk 的上游，功能上被 fpk 完全覆盖（fpk 多了界面/规格探测/应用封装）。
**没必要换用 B**，直接用 fpk 1.3.1 即可。

### 无论哪种，都建议做的三件事
1. **采纳 A 的 `getOpenNewAssortment` 分组**（分组名 + 多组归属，明显优于规则推断）
2. **采纳 A 的 13 市交集剔除算法**（识别省级台更准）
3. **不要并行运行** systemd 版与 fpk 版

---

*本报告所有结论基于实际代码阅读与 NAS 实测，回看地址两种格式均已实跑验证。*
*归档：`upstream/7bcb3d91-fongmi-v4/`、`upstream/e2a50ea8-tvbox/`*
