#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
江苏移动 IPTV · 飞牛 fnOS 应用服务端

功能：
  1. 定时（默认 03:30）从江苏移动 OTT lvps 接口拉全量频道表 + 节目单
  2. 生成 iptv.m3u8 / epg.xml / tvbox.json（可切换完整模式输出更多格式）
  3. 内置 HTTP 服务：数据文件分发 + 频道预览 API + Web UI
  4. 台标本地化、抽样自检、原子发布、CDN 自动切换

纯标准库实现，无第三方依赖。
"""

import argparse
import concurrent.futures as cf
import csv
import fcntl
import hashlib
import io
import zlib
import struct
import http.server
import http.client
import json
import os
import random
import re
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
import urllib.error
import urllib.parse
import urllib.request

try:
    import media_probe as _mp
except Exception:                       # 打包后若模块缺失，只是少一个规格显示，不影响播放
    _mp = None

# 流规格缓存：同一频道 30 分钟内不重复探测（探测要下载一个切片，约 1–3 秒）
_SPEC_CACHE = {}
_SPEC_TTL = 1800
_spec_lock = threading.Lock()
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape


def _xml_attr(value):
    return escape(str(value), {'"': "&quot;"})

# ---------------------------------------------------------------- 常量

EPG_BASE = "http://lvpepg.uni.jsa.bcs.ottcn.com:8080/cms-lvp-epg"
ASSORT_ID = "115"
TZ8 = timezone(timedelta(hours=8))
UA = {"User-Agent": "okhttp/3.12.0"}

HOSTS = [
    "223.110.247.231:6610",
    "223.110.247.216:6610",
    "223.110.247.195:6610",
    "223.110.247.151:6610",
    "223.110.247.137:6610",
    "223.110.247.134:6610",
    "223.110.247.130:6610",
    "223.110.247.200:6610",
    "223.110.247.228:6610",
    "223.110.247.203:6610",
    "223.110.247.211:6610",
    "223.110.247.217:6610",
]
SCAN_RANGE = ("223.110.247.", 100, 255)

JIANGSU = [
    ("320100", "南京"), ("320200", "无锡"), ("320300", "徐州"), ("320400", "常州"),
    ("320500", "苏州"), ("320600", "南通"), ("320700", "连云港"), ("320800", "淮安"),
    ("320900", "盐城"), ("321000", "扬州"), ("321100", "镇江"), ("321200", "泰州"),
    ("321300", "宿迁"),
]
DNAME = dict(JIANGSU)

# 分组顺序：4K 置顶，其后 央视 → 卫视 → 江苏 → 地方-xx → 其它。
# 1.6.0 起「其它」不再是一坨 134 台，而是按内容拆成 6 个二级组：
#   影视剧场 / 少儿动画 / 体育竞技 / 生活纪录 / 音乐广播 / 购物。仍排在最后。
GROUP_UHD = "4K"
GROUP_ORDER = (GROUP_UHD, "央视", "卫视", "江苏", "地方",
               "影视剧场", "少儿动画", "体育竞技", "生活纪录", "音乐广播", "购物")

# 运营商「电视墙」原版分组（getOpenNewAssortment 实测 assortId → assortName）。
# 只用于二级兜底映射，不参与主分组判定（主分组由「频道名 + otype」判定，见 _classify）。
ASSORT_GROUPS = {208: GROUP_UHD, 116: "央视", 121: "卫视", 120: "江苏",
                 170: "体育竞技", 211: "生活纪录", 169: "影视剧场", 171: "少儿动画",
                 172: "生活纪录", 122: "音乐广播", 173: "购物"}
ASSORT_ALL = "115"
ABILITY_BASE = {"deviceGroupIds": ["1697"]}

# ---- 方案二：上游原版分组（完全照搬电视墙，不做任何本地化重排）----
# 顺序即电视墙顺序；「全部/收藏」不是分组，不产出。
ORIG_GROUPS = [
    ("208", "超高清"), ("116", "央视"), ("120", "本地"), ("121", "卫视"),
    ("170", "体育"), ("211", "专业频道"), ("169", "影视"), ("171", "少儿"),
    ("172", "生活"), ("122", "广播"), ("173", "购物"),
]
# 上游「超高清」里混着高清臻享（部分仅 1080P），原版清单按上游原样输出不谎报，
# 只在 EXTINF 里如实带分辨率信息，由用户自行判断。

# 4K/超高清判定。
#
# 1.6.0 的重要修正 —— 「高清臻享」组：
#   用户要求把「高清臻享」频道统一并入 4K 组。但实测发现其中只有一部分是真 4K：
#     CCTV1/3/5高清臻享 → 3840x2160 真 4K
#     CCTV2/6/8高清臻享、睛彩篮球/竞技/青少高清臻享 → 1920x1080（并非 4K）
#   因此这里分两层：
#     · 「真 4K」= 宽高实测达到 3840x2160 → is_4k=True；源标记只用于候选。
#     · 「高清臻享」= 名称/清晰度名带「高清臻享」            → grp_uhd=True（进 4K 组，但不冒充真 4K）
#   界面按 is_4k 打「4K」徽标、按真实 w/h 展示档位，避免把 1080P 谎报成 4K。
#
# 注意 UHD_CODE_RE 以前会把 AVS 编码名里的「4K264」当成 4K 标记（G_CCTV-1-AVS-4K264），
# 已收紧为「4K/2160 且后面不再紧跟数字」，避免 4K264 误命中。
UHD_CODE_RE = re.compile(r"(?:4K|UHD|2160)(?![0-9])", re.I)
UHD_NAME_RE = re.compile(r"4K|UHD|超高清", re.I)
ZHENXIANG_RE = re.compile(r"高清臻享")

# 各播放器的回看占位符语法互不兼容，因此按播放器分套产出：
#   ap                 ${(b)yyyyMMddHHmmss}   APTV / TVBox / DIYP / FongMi / 酷9 通用
#   kodi               ${start}               Kodi pvr.iptvsimple 官方语法
#   fongmi             ${(b)yyyyMMddHHmmss}   与 ap 同，但单独落文件便于电视端点选
# 注意：裸 {start}/{end} 只有极少数播放器认，APTV 不认，主入口不能再用它。
STYLES = (
    ("ap", "直播源.m3u", "${(b)yyyyMMddHHmmss}", "${(e)yyyyMMddHHmmss}"),
    ("kodi", "直播源_kodi.m3u", "${start:YmdHMS}", "${end:YmdHMS}"),
    ("fongmi", "直播源_tvbox_fongmi.m3u", "${(b)yyyyMMddHHmmss}", "${(e)yyyyMMddHHmmss}"),
)

PRIVACY_PATTERNS = [
    (r"1[3-9]\d{9}", "手机号"),
    (r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", "MAC 地址"),
    (r"00411300\d{10}", "STBID"),
    (r"OTTUserToken|IASHttpSessionId|accountinfo|UserName", "账号令牌字段"),
]

DEFAULTS = {
    "port": 18123,
    "district": "all",
    "update_at": "03:30",
    "epg_enable": True,
    "days": 3,
    "history_days": 7,
    "backup_days": 7,
    "localize_logo": True,
    "schedule_enabled": True,
    "output_mode": "minimal",
}

# ---------------------------------------------------------------- 路径与配置

APPDEST = os.environ.get("TRIM_APPDEST", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKGETC = os.environ.get("TRIM_PKGETC", os.path.join(APPDEST, "etc"))
PKGVAR = os.environ.get("TRIM_PKGVAR", os.path.join(APPDEST, "var"))
WWW = os.path.join(APPDEST, "www")
DATA = os.path.join(PKGVAR, "data")
CONF_FILE = os.path.join(PKGETC, "config.json")

# 老版本直接在 data/ 写文件；首代成功前继续读取旧布局，不就地迁移旧数据。
def _output_dir():
    pointer = os.path.join(DATA, ".current")
    return os.path.realpath(pointer) if os.path.islink(pointer) else DATA


def _runtime_version():
    try:
        with open(os.path.join(WWW, "VERSION"), encoding="utf-8") as f:
            return f.read(64).strip() or None
    except OSError:
        # Source-tree launches have no build-generated VERSION; installed apps do.
        manifest = os.path.join(os.path.dirname(APPDEST), "manifest")
        try:
            with open(manifest, encoding="utf-8") as f:
                return next((line.partition("=")[2].strip() for line in f if line.startswith("version=")), None)
        except OSError:
            return None

_lock = threading.Lock()
_updating = {"running": False, "started": 0, "error": ""}


def log(*a):
    msg = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(x) for x in a))
    print(msg, flush=True)
    try:
        os.makedirs(PKGVAR, exist_ok=True)
        with open(os.path.join(PKGVAR, "app.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    # 类型归一
    cfg["epg_enable"] = str(cfg.get("epg_enable", "true")).lower() in ("1", "true", "yes", "on")
    cfg["localize_logo"] = str(cfg.get("localize_logo", "true")).lower() in ("1", "true", "yes", "on")
    for k in ("days", "history_days", "backup_days", "port"):
        try:
            cfg[k] = int(cfg[k])
        except Exception:
            cfg[k] = DEFAULTS[k]
    return cfg


def save_config(cfg):
    """原子写配置"""
    os.makedirs(os.path.dirname(CONF_FILE), exist_ok=True)
    tmp = CONF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF_FILE)


def _tail_log(n=120):
    """读服务日志尾部，供界面展示"""
    for p in (os.path.join(PKGVAR, "service.log"), os.path.join(PKGVAR, "app.log")):
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                return "".join(lines[-n:])
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------- HTTP 取数

def http_get(url, timeout=25, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise last


def http_get_bytes(url, timeout=8, retries=1):
    last = None
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(0.2)
    raise last


def lan_ip():
    """探测本机在局域网中的地址，用于生成外部可访问的绝对 URL。"""
    for target in (("223.110.247.231", 6610), ("223.5.5.5", 53), ("192.168.1.1", 53)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.5)
            s.connect(target)
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            continue
    # 退回枚举网卡地址
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- 台标本地化

def _png_pixels(path, max_side=64):
    """纯标准库解析 PNG，返回 [(r,g,b,a), ...]；不支持时返回 None。
    只处理常见 8bit RGB/RGBA/灰度/调色板，足够判断台标明暗。"""
    try:
        raw = open(path, "rb").read()
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        pos = 8
        w = h = bitdepth = colortype = None
        idat = []
        palette = None
        trns = None
        while pos < len(raw):
            ln = struct.unpack(">I", raw[pos:pos + 4])[0]
            typ = raw[pos + 4:pos + 8]
            data = raw[pos + 8:pos + 8 + ln]
            pos += 12 + ln
            if typ == b"IHDR":
                w, h, bitdepth, colortype = struct.unpack(">IIBB", data[:10])
                if data[12] != 0:   # interlace，不处理
                    return None
            elif typ == b"PLTE":
                palette = data
            elif typ == b"tRNS":
                trns = data
            elif typ == b"IDAT":
                idat.append(data)
            elif typ == b"IEND":
                break
        if not idat or bitdepth != 8:
            return None
        buf = zlib.decompress(b"".join(idat))
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colortype)
        if channels is None:
            return None
        stride = w * channels
        out = []
        prev = bytearray(stride)
        i = 0
        # 缩放采样步长（最多取 64x64 个点）
        sx = max(1, w // max_side)
        sy = max(1, h // max_side)
        for y in range(h):
            if i + 1 + stride > len(buf):
                break
            ft = buf[i]; i += 1
            line = bytearray(buf[i:i + stride]); i += stride
            # 反滤波
            if ft == 1:
                for x in range(channels, stride):
                    line[x] = (line[x] + line[x - channels]) & 0xFF
            elif ft == 2:
                for x in range(stride):
                    line[x] = (line[x] + prev[x]) & 0xFF
            elif ft == 3:
                for x in range(stride):
                    a = line[x - channels] if x >= channels else 0
                    line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
            elif ft == 4:
                for x in range(stride):
                    a = line[x - channels] if x >= channels else 0
                    b = prev[x]
                    c = prev[x - channels] if x >= channels else 0
                    pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[x] = (line[x] + pr) & 0xFF
            prev = line
            if y % sy:
                continue
            for x in range(0, w, sx):
                off = x * channels
                if colortype == 6:
                    out.append((line[off], line[off + 1], line[off + 2], line[off + 3]))
                elif colortype == 2:
                    out.append((line[off], line[off + 1], line[off + 2], 255))
                elif colortype == 0:
                    v = line[off]; out.append((v, v, v, 255))
                elif colortype == 4:
                    v = line[off]; out.append((v, v, v, line[off + 1]))
                elif colortype == 3 and palette:
                    pi = line[off]
                    if pi * 3 + 2 < len(palette):
                        al = trns[pi] if (trns and pi < len(trns)) else 255
                        out.append((palette[pi * 3], palette[pi * 3 + 1], palette[pi * 3 + 2], al))
        return out
    except Exception:
        return None


def _logo_tone(path):
    """判断台标内容偏亮还是偏暗 -> 前端据此选深底/浅底。
    运营商台标多为透明底+白色内容（需深底），少数为深色内容（需浅底）。
    返回 'light'(内容亮→深底) / 'dark'(内容暗→浅底)。"""
    px = _png_pixels(path)
    if not px:
        return "light"
    tot = 0.0
    n = 0
    for r, g, b, a in px:
        if a > 40:
            tot += 0.299 * r + 0.587 * g + 0.114 * b
            n += 1
    if n == 0:
        return "light"
    return "light" if (tot / n) >= 100 else "dark"


def localize_logos(chs, base_url, logo_dir, previous_logo_dir):
    if not base_url:
        return 0
    base = base_url.rstrip("/")
    os.makedirs(logo_dir, exist_ok=True)

    def one(ch):
        u = ch.get("logo") or ""
        if not u:
            return False
        fn = hashlib.md5(u.encode("utf-8")).hexdigest()[:12] + ".png"
        p = os.path.join(logo_dir, fn)
        if not (os.path.exists(p) and os.path.getsize(p) > 200):
            try:
                cached = os.path.join(previous_logo_dir, fn)
                if os.path.isfile(cached) and os.path.getsize(cached) > 200:
                    shutil.copyfile(cached, p)
                else:
                    data = http_get_bytes(u)
                    if len(data) < 200:
                        return False
                    with open(p, "wb") as f:
                        f.write(data)
            except Exception:
                return False
        ch["logo_tone"] = _logo_tone(p)
        # 台标一律存**相对路径**，不写死 host。
        # 写绝对地址（http://某IP:端口/logo/..）会导致：用域名访问、换 IP、
        # 或在飞牛桌面 iframe 里访问时图片加载失败，只露出深色底 -> 视觉「纯黑方块」。
        # 相对路径由浏览器按当前源解析，任何访问方式都能取到图。
        ch["logo_local"] = "/logo/" + fn
        ch["logo"] = "/logo/" + fn
        ch["logo_abs"] = base + "/logo/" + fn
        return True

    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        got = sum(1 for r in ex.map(one, chs) if r)
    return got


# ---------------------------------------------------------------- 数据抓取

def load_ability(district="320600"):
    d = dict(ABILITY_BASE)
    d["districtCode"] = district
    return urllib.parse.quote(json.dumps(d, ensure_ascii=False, separators=(",", ":")), safe="")


def ordered_hosts(cache_file):
    def read_lines(p):
        out = []
        if os.path.exists(p):
            try:
                for l in open(p, encoding="utf-8").read().splitlines():
                    l = l.split("#")[0].strip()
                    if l:
                        out.append(l)
            except Exception:
                pass
        return out

    hs = []
    if os.path.exists(cache_file):
        hs += read_lines(cache_file)[:1]
    hs += HOSTS
    hs += read_lines(os.path.join(PKGVAR, "hosts.txt"))
    hs += [h.replace(":6610", ":6410") for h in hs if h.endswith(":6610")]
    out = []
    for h in hs:
        if h not in out:
            out.append(h)
    return out


def discover():
    pre, lo, hi = SCAN_RANGE
    cands = ["%s%d:%d" % (pre, i, p) for i in range(lo, hi + 1) for p in (6610, 6410)]
    log("扫描 %d 个候选 CDN 地址 ..." % len(cands))

    def probe(h):
        try:
            b = http_get("http://%s/gitv/live1/G_CCTV-1-HQ/G_CCTV-1-HQ/1.m3u8" % h, timeout=3, retries=0)
            return h if "#EXTM3U" in b else None
        except Exception:
            return None

    found = []
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        for r in ex.map(probe, cands):
            if r:
                found.append(r)
    log("发现可用 CDN %d 个: %s" % (len(found), found[:8]))
    if found:
        p = os.path.join(PKGVAR, "hosts.txt")
        old = set()
        if os.path.exists(p):
            old = set(l.split("#")[0].strip() for l in open(p, encoding="utf-8"))
        with open(p, "a", encoding="utf-8") as f:
            f.write("# --discover %s 扫到\n" % time.strftime("%Y-%m-%d %H:%M"))
            for h in found:
                if h not in old:
                    f.write(h + "\n")
    return found


LIVE_URL_RE = re.compile(r"/(gitv|cntv|jsyd)/(?:[^/]+/)?(?:live|live1)/([^/]+)/1\.m3u8")


def parse_stream_url(u):
    """从运营商 resolution[].url 取 (cp, code)。
    源地址里的主机被脱敏成 XXX.XXX.XXX.XXX，只有 cp 与 code 可用，
    真正播放时用自己挑选的 CDN 主机拼出来。"""
    m = LIVE_URL_RE.search(u or "")
    if m:
        return m.group(1), m.group(2)
    # 兜底：老格式 .../live/<code>/1.m3u8
    m = re.search(r"/live1?/([^/]+)/1\.m3u8", u or "")
    if m:
        m2 = re.search(r"/(gitv|cntv|jsyd)/", u or "")
        return (m2.group(1) if m2 else "gitv"), m.group(1)
    return None, None


def _resolutions_of(c):
    """把某频道所有清晰度档解析成结构化列表，按运营商权重降序（weight 越大越好）。"""
    out = []
    for r in c.get("resolution") or []:
        cp, code = parse_stream_url(r.get("url"))
        if not code:
            continue
        logo = ""
        for p in r.get("poster") or []:
            if p.get("posterType") == 103 and p.get("posterUrl"):
                logo = p["posterUrl"]
                break
        if not logo:
            for p in r.get("poster") or []:
                if p.get("posterUrl"):
                    logo = p["posterUrl"]
                    break
        out.append({
            "name": r.get("name") or "", "cp": cp, "code": code,
            "weight": int(r.get("weight") or 0),
            "logo": logo,
            "mc": r.get("multicastUrl") or "",
            "is_default": (r.get("name") == c.get("defaultResolution")),
        })
    out.sort(key=lambda v: (-v["weight"], 0 if v["is_default"] else 1))
    return out


def _is_uhd_res(r):
    """某一路清晰度是不是「高画质候选档」。
    既认名称（高清臻享 / 超高清 / 4K），也认代码里的硬 4K 标记（G_xxx-4K）。
    注意：代码里的「4K264」是 AVS 编码命名，并不代表真 4K，故 UHD_CODE_RE 已收紧，
    真伪最终由 probe_uhd 实测分辨率裁定。"""
    nm = (r.get("name") or "")
    if "高清臻享" in nm or "超高清" in nm or "4K" in nm.upper():
        return True
    return bool(UHD_CODE_RE.search(r.get("code") or ""))


def pick_uhd(res_list):
    """从清晰度档里挑最高画质的那一档（高清臻享优先，其次权重高者）；
    没有高画质档就返回 None，交给默认档。"""
    hit = [r for r in res_list if _is_uhd_res(r)]
    if not hit:
        return None
    # 高清臻享优先；同档里权重高者优先
    hit.sort(key=lambda v: (0 if "高清臻享" in (v["name"] or "") else 1, -v["weight"]))
    return hit[0]


def build_channels(ability, district="", quiet=False, group_map=None, epg_enable=True):
    """拉取某地市/某分组下的频道。

    group 的取值（关键修正）：
      · 主分组**完全来自上游接口** getOpenNewAssortment 的原版分组归属
        （见 collect()，这里只填 assort 入参用于拉取）。
      · 单个 channelList 请求内先给一个「原分组」初值，最终由 collect() 覆盖。
    """
    assort = ASSORT_ALL if group_map is None else str(group_map)
    if not quiet:
        log("[1/5] 拉频道表 assort=%s (%s %s) ..." % (assort, district, DNAME.get(district, "")))
    url = ("%s/lvps/channelList?assortId=%s&serviceChannelId=&isShowNewProgram=%s&abilityString=%s"
           % (EPG_BASE, assort, "true" if epg_enable else "false", ability))
    j = json.loads(http_get(url))
    if j.get("resultCode") != "000":
        raise RuntimeError("channelList 失败: %s" % j.get("resultMessage"))
    out = []
    for c in (j.get("content") or {}).get("channels") or []:
        rl = _resolutions_of(c)
        if not rl:
            continue
        # 默认档：运营商指定的清晰度；用于普通频道
        dflt = next((r for r in rl if r["is_default"]), rl[0])
        # 高画质档（可能是高清臻享 / 真 4K）：优先用它（修「名字叫 4K 却播 1080P」）
        uhd = pick_uhd(rl)
        chosen = uhd or dflt
        name = c.get("name") or ""
        num = str(c.get("num") or "")
        # 「高清臻享」= 该频道名或所选档名带「高清臻享」→ 归入 4K 组（但不等于真 4K）
        zhx = bool(ZHENXIANG_RE.search(name)) or ("高清臻享" in (chosen["name"] or ""))
        out.append({
            "num": num, "name": name, "uuid": c.get("uuid"),
            "code": chosen["code"], "cp": chosen["cp"],
            "back": int(c.get("isShowBack") or 0), "mc": chosen["mc"],
            "logo": chosen["logo"], "group": "其他", "district": district,
            # ---- 4K / 多档相关
            # is_4k   = 真 4K（probe_uhd 实测后才有定论，这里先按代码标记估）
            # grp_uhd = 归入 4K 组（真 4K 或 高清臻享）
            "is_4k": bool(uhd) and bool(UHD_CODE_RE.search(chosen["code"] or "")),
            "grp_uhd": zhx or (bool(uhd) and bool(UHD_CODE_RE.search(chosen["code"] or ""))),
            "zhx": zhx,                                   # 是否高清臻享
            "uhd_name": (uhd or {}).get("name", ""),      # 例如「高清臻享」
            "res_name": chosen["name"],                   # 例如「超清」「标清」
            "alts": [{"name": r["name"], "cp": r["cp"], "code": r["code"]}
                     for r in rl if not (r["code"] == chosen["code"] and r["cp"] == chosen["cp"])],
            # 4K 档播出不了时的备选（1080P 档），播放器/界面自动降档用
            "fallback": "" if not uhd else next(
                ({"cp": r["cp"], "code": r["code"], "name": r["name"]}
                 for r in rl if not _is_uhd_res(r)), ""),
        })
    if not quiet:
        log("      频道 %d（4K %d）" % (len(out), sum(1 for c in out if c["is_4k"])))
    return out


def fetch_assortments(ability):
    """上游原版分组清单（电视墙）。返回 [(assortId, assortName, sortNum)]，已剔除「全部/收藏/最新」。"""
    url = "%s/lvps/getOpenNewAssortment?serviceChannelId=&abilityString=%s" % (EPG_BASE, ability)
    try:
        j = json.loads(http_get(url))
        lst = (j.get("content") or {}).get("assortList") or []
        gs = [(str(x["assortId"]), x.get("assortName") or "", int(x.get("sortNum") or 99))
              for x in lst
              if str(x.get("assortId", "")).isdigit()
              and x.get("assortName") not in ("全部", "收藏", "最新")]
        if gs:
            return sorted(gs, key=lambda v: v[2])
    except Exception as e:      # noqa: BLE001
        log("      分组接口失败(%s)，用内置表" % e)
    return [(str(i), n, k + 1) for k, (i, n) in enumerate(sorted(ASSORT_GROUPS.items()))]


def build_programs(channels, ability, hist_days, days, quiet=False):
    if not quiet:
        log("[2/5] 拉节目单（过去 %d 天 ~ 未来 %d 天）..." % (hist_days, days))
    today = datetime.now(TZ8)
    base = ("%s/lvps/getAllProgramlist?startDate=%s&endDate=%s&pos=fullplayer&allDay=true"
            "&channelType=&relatedId=&copyrightTip=&serviceChannelId=&abilityString=%s"
            % (EPG_BASE, (today - timedelta(days=hist_days)).strftime("%Y%m%d"),
               (today + timedelta(days=days)).strftime("%Y%m%d"), ability))
    progs, done, failed, invalid = [], 0, 0, 0

    def one(ch):
        try:
            j = json.loads(http_get(base + "&" + urllib.parse.urlencode({"uuid": ch["uuid"]}), timeout=12, retries=1))
            if j.get("resultCode") not in (None, "000") or not isinstance(j.get("content"), list):
                return [], True, 0
        except (OSError, ValueError, AttributeError):
            return [], True, 0
        rows, skipped, seen = [], 0, set()
        for day in j["content"]:
            if not isinstance(day, dict) or not isinstance(day.get("programs"), list):
                skipped += 1
                continue
            for p in day["programs"]:
                try:
                    title = p["programName"].strip()
                    start, end = int(p["startTime"]), int(p["endTime"])
                    if not title or not 0 < start < end < 2**32:
                        raise ValueError()
                except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
                    skipped += 1
                    continue
                key = (start, end, title)
                if key not in seen:
                    seen.add(key)
                    rows.append({"uuid": ch["uuid"], "title": title, "start": start, "end": end})
        return rows, False, skipped

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for f in cf.as_completed([ex.submit(one, c) for c in channels]):
            rows, failure, skipped = f.result()
            progs.extend(rows)
            failed += failure
            invalid += skipped
            done += 1
            if not quiet and done % 60 == 0:
                log("      %d/%d" % (done, len(channels)))
    if failed or invalid or not quiet:
        log("      节目 %d 条；请求失败 %d/%d，忽略异常记录 %d" % (len(progs), failed, done, invalid))
    if done >= 10 and failed * 5 > done * 2:
        raise RuntimeError("节目单请求失败超过 40%%（%d/%d），保留上一版" % (failed, done))
    return progs


def fetch_members(ability, assort):
    """只取某上游分组下的 (uuid, name)，用于建立分组归属表。"""
    url = ("%s/lvps/channelList?assortId=%s&serviceChannelId=&abilityString=%s"
           % (EPG_BASE, assort, ability))
    j = json.loads(http_get(url))
    if j.get("resultCode") != "000":
        return []
    return [c.get("uuid") for c in (j.get("content") or {}).get("channels") or [] if c.get("uuid")]


def fetch_islocal(dc="320000"):
    """采「全省视角」的 isLocal 标识 → {uuid: 频道名}，即全部县区级台。

    为什么用它（2026-11-25 实测决策）：
      · 上游每个频道对象自带 isLocal 字段，由源站自己维护，新增县区台会自动打标；
      · 旧方案靠「13 个地市本地清单取交集剔除」，要求 13 份清单同时更新且全部
        拉取成功才算得出结果，任一地市接口失败或漏推 → 该台掉进「省级台」被误判；
      · 实测两法当前结果 100% 一致（全省视角 59 台 vs 交集法 59 台），
        但 isLocal 更抗上游变化，因此改为主判据、交集法降级为交叉校验。

    ⚠️ 必须用 districtCode=320000（全省）。用某个地市视角会只返回该市自己的
    本地台（南通仅 3 台、南京 4 台），地方组会从 59 台塌成几个。
    """
    url = ("%s/lvps/channelList?assortId=115&serviceChannelId=&abilityString=%s"
           % (EPG_BASE, load_ability(dc)))
    j = json.loads(http_get(url))
    if j.get("resultCode") != "000":
        return {}
    return {c.get("uuid"): (c.get("name") or "")
            for c in (j.get("content") or {}).get("channels") or []
            if str(c.get("isLocal")) == "1" and c.get("uuid")}


def _classify(c):
    """给频道定最终分组（唯一权威入口，供 collect / _reclassify 复用）。

    判定顺序（越靠前优先级越高）：
      1. 归入 4K 组：真 4K（is_4k）、 「高清臻享」（zhx）或待探测的高画质候选—— 用户要求高清臻享也进 4K 组
      2. 地方台：13 地市清单独有 → 「地方」
      3. 按上游原分组名（otypes）：
         央视 → 央视；卫视 → 卫视；本地 → 江苏
         体育 → 体育竞技；专业频道 → 生活纪录；影视 → 影视剧场；
         少儿 → 少儿动画；生活 → 生活纪录；广播 → 音乐广播；购物 → 购物
      4. 兜底：用频道名特征再分一次（otypes 缺失时有 12 个台），最后落「生活纪录」
    """
    if c.get("is_4k") or c.get("zhx") or c.get("uhd_unverified"):
        return GROUP_UHD
    if c.get("group_from_city"):
        return "地方"
    gl = c.get("otypes") or []
    # 这些 otype 语义明确，直接映射（名字五花八门，靠名称会漏，如剧名「狂飙」）
    for raw, dst in (("央视", "央视"), ("卫视", "卫视"), ("本地", "江苏"),
                     ("影视", "影视剧场"), ("少儿", "少儿动画"), ("体育", "体育竞技"),
                     ("广播", "音乐广播"), ("购物", "购物")):
        if raw in gl:
            return dst
    # 「专业频道 / 生活 / 空」内容很杂，改由名称细分
    # （专业频道里有 第一剧场/风云足球/风云音乐/第一财经 等，不能一概归生活纪录）
    return _classify_by_name(c)


# 名称兜底规则：otypes 缺失（实测有 12 个）或上游没归类时，按频道名再判一次。
_CCTV_NUM_RE = re.compile(r"^CCTV[- ]?(\d+)", re.I)
def _classify_by_name(c):
    n = c.get("name") or ""
    if _CCTV_NUM_RE.match(n) or n.startswith("央视") or "CGTN" in n.upper() \
            or "英语新闻" in n:
        return "央视"
    if "卫视" in n:
        return "卫视"
    if "广播" in n or "之声" in n or "调频" in n or "音乐" in n:
        return "音乐广播"
    if re.search(r"财经|财金|股市|证券", n):
        return "生活纪录"
    if re.search(r"少儿|卡通|动画|佩奇|飞侠|奥特曼|熊出没|儿童", n):
        return "少儿动画"
    if re.search(r"体育|足球|篮球|网球|围棋|赛事|竞技|功夫|棋牌", n):
        return "体育竞技"
    if re.search(r"购|好物|养生|保健|食品|饮食|收藏|文旅", n):
        return "购物"
    if re.search(r"影视|剧场|电影|院线|戏曲|梨园|综艺|喜剧", n):
        return "影视剧场"
    return "生活纪录"


# 孤儿频道 = 上游「全部」里存在、但没被放进任何原版分组的频道。
# 它们没有 otype，只能走 _classify_by_name 猜。上游随时可能新增这类台，
# 静默塞进「生活纪录」很难察觉，因此这里显式收集 + 告警（方案一）。
ORPHANS = {}


def _collect_orphans(chs):
    """收集没有上游分组归属的频道，供日志告警与 meta 记录。

    实测（2026-11-25）：「全部」278 台里有 12 台上游未归任何分组
    （高尔夫网球/地方戏曲/欢乐棋牌/潮妈辣婆/央视I套/军事之旅/科幻世界/
      儿童天地/纪录片/梨园经典荟萃/第一院线/影视剧场），当前靠名称兜底。
    """
    ORPHANS.clear()
    for c in chs:
        if not (c.get("otypes") or []):
            ORPHANS[c["uuid"]] = {
                "num": c.get("num"), "name": c.get("name"),
                "group": c.get("group"), "by": "名称兜底",
            }
    if ORPHANS:
        log("      ⚠ 孤儿频道（上游未归任何分组，靠名称兜底）%d 个：%s"
            % (len(ORPHANS), "、".join("%s→%s" % (o["name"], o["group"])
                                      for o in list(ORPHANS.values())[:10])))
        if len(ORPHANS) > 5:
            log("      ⚠ 孤儿数量 %d > 5，上游可能新增了未分类频道，建议核对分组"
                % len(ORPHANS))
    return ORPHANS


def probe_uhd(chs, cdn, quiet=False):
    """实测「高画质候选」的真实分辨率，据此判定真 4K。
    候选 = 名称带 4K/UHD/超高清/高清臻享，或代码带 4K 标记，或已标记为高清臻享的台。
    """
    cand = [c for c in chs
            if UHD_NAME_RE.search(c["name"] or "") or ZHENXIANG_RE.search(c["name"] or "")
            or UHD_CODE_RE.search(c["code"] or "") or c.get("grp_uhd") or c.get("zhx")]
    if not cand:
        return
    for c in cand:
        c["is_4k"] = False
        c["uhd_unverified"] = True
    if _mp is None:
        return

    def one(c):
        # 直播切片偶发取到正在写入/损坏的那一片，导致解析不到 SPS。
        # 重试最多 3 次（每次重新拉 m3u8 会换到新的切片），显著降低误判。
        url = "%s/%s/live1/%s/%s/1.m3u8" % (cdn, c["cp"], c["code"], c["code"])
        for attempt in range(3):
            try:
                info = _mp.probe_url(url, timeout=20, max_bytes=8000000)
                if info and (info.get("video") or {}).get("w"):
                    return info
            except Exception:
                pass
            time.sleep(0.3 + attempt * 0.4)
        return None

    def apply(c, info):
        v = ((info or {}).get("video") or {})
        w, h = int(v.get("w") or 0), int(v.get("h") or 0)
        if w and h:
            c["w"], c["h"] = w, h
            c["codec"] = v.get("codec") or ""
            c["vprofile"] = v.get("profile") or ""
            # 真 4K 唯一判据：实测 >= 3840x2160。高清臻享若实测只有 1080P，不算真 4K。
            c["is_4k"] = w >= 3840 and h >= 2160
            c["uhd_unverified"] = False
            return True
        return False

    ok = 0
    pending = []
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for c, info in zip(cand, ex.map(one, cand)):
            if apply(c, info):
                ok += 1
            else:
                pending.append(c)
    # 并发下个别大码率 4K 流（如 CCTV-164K 8MB 才拿到 SPS）会因抢占带宽失败，
    # 失败项再串行重试一轮，带宽独占后基本都能成功。
    if pending:
        for c in pending:
            for _ in range(2):
                if apply(c, one(c)):
                    ok += 1
                    break
                time.sleep(1.0)
    if not quiet:
        n4k = sum(1 for c in cand if c.get("is_4k"))
        nzx = sum(1 for c in cand if c.get("zhx"))
        log("      高画质实测 %d/%d 成功；真 4K %d 个，高清臻享 %d 个（其中真 4K %d）"
            % (ok, len(cand), n4k, nzx, sum(1 for c in cand if c.get("zhx") and c.get("is_4k"))))


_QUALITY_AFFIX = re.compile(r"^(?:高清臻享|超高清|UHD|4K)|(?:高清臻享|超高清|UHD|4K|2160P|1080P|高清|标清)$", re.I)


def _uhd_channel_key(name):
    """仅去除名称两端的画质标记，保留频道名称和 + 等业务标记。"""
    key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or "")).upper()
    while key:
        stripped = _QUALITY_AFFIX.sub("", key).strip("-_()【】[]")
        if stripped == key:
            break
        key = stripped
    if key in {"央视I套", "央视一套", "央视1套"}:
        return "CCTV-1"
    return re.sub(r"^CCTV-?(\d+)$", r"CCTV-\1", key)


def _uhd_rank(c):
    return (not c.get("is_4k"), bool(c.get("uhd_unverified")), not c.get("zhx"),
            -int(c.get("h") or 0), -int(c.get("back") or 0),
            int(re.sub(r"\D", "", c.get("num") or "") or 999999), c["uuid"])


def _unique_uhd(channels):
    """从同逻辑频道或同一路流中择优，返回胜出频道及其余档位。"""
    by_name, by_stream, winners, alternatives = {}, {}, [], []
    for c in sorted(channels, key=_uhd_rank):
        name = _uhd_channel_key(c.get("name"))
        stream = (c.get("cp"), c.get("code")) if c.get("code") else None
        winner = (by_name.get(name) if name else None) or (by_stream.get(stream) if stream else None)
        if winner is None:
            winner = c
            winners.append(c)
        else:
            alternatives.append((winner, c))
        if name:
            by_name.setdefault(name, winner)
        if stream:
            by_stream.setdefault(stream, winner)
    return winners, alternatives


def _add_live_alternative(winner, variant):
    """保留备选直播。独立 UUID 的回看与节目单由其原版条目承载。"""
    alts = winner.setdefault("alts", [])
    seen = {(a["cp"], a["code"]) for a in alts} | {(winner["cp"], winner["code"])}
    source = {"name": variant.get("res_name") or variant["name"], "cp": variant["cp"], "code": variant["code"]}
    if variant.get("h") == 1080:
        source["name"] = "1080P 臻享" if variant.get("zhx") else "1080P"
    for alt in [source, *(variant.get("alts") or [])]:
        key = (alt["cp"], alt["code"])
        if key not in seen:
            alts.append(dict(alt))
            seen.add(key)


def _reclassify_uhd(chs):
    """实测后生成智能分组。原版所用的上游频道集合由调用方保留。"""
    for c in chs:
        c["group"] = _classify(c)
        key = _uhd_channel_key(c.get("name"))
        if c.get("is_4k") and re.fullmatch(r"CCTV-\d+", key):
            c["name"] = key + " 4K"
    _, variants = _unique_uhd(c for c in chs if c["group"] == GROUP_UHD)
    dropped = set()
    for winner, variant in variants:
        _add_live_alternative(winner, variant)
        # 保留央视组的一套普通入口，有实测 1080p 时优先使用这一路。
        if (_uhd_channel_key(winner["name"]) == _uhd_channel_key(variant["name"]) == "CCTV-1"
                and winner.get("is_4k") and variant.get("h") == 1080 and not variant.get("is_4k")):
            variant.update(group="央视", name="CCTV-1", num="1", sort_num=1)
        else:
            dropped.add(variant["uuid"])
    cctv1 = [c for c in chs if c["group"] == "央视" and _uhd_channel_key(c["name"]) == "CCTV-1"]
    if cctv1:
        main, *others = sorted(cctv1, key=lambda c: (-int(c.get("h") or 0), _uhd_rank(c)))
        main.update(name="CCTV-1", num="1", sort_num=1)
        for variant in others:
            _add_live_alternative(main, variant)
            dropped.add(variant["uuid"])
    if dropped:
        chs[:] = [c for c in chs if c["uuid"] not in dropped]
        log("      智能清单合并 %d 条重复档位" % len(dropped))


def collect(districts, days, hist_days=7, epg_enable=True):
    """拉全量频道 + 节目单，并按**上游原版分组**归类。

    分组链路（回答「分类名是否完全来自上游」）：
      · 上游 getOpenNewAssortment 只给出 11 个粗分组
        （超高清 / 央视 / 本地 / 卫视 / 体育 / 专业频道 / 影视 / 少儿 / 生活 / 广播 / 购物），
        它分不出「江苏省级台」和「13 个地市的地方台」。
      · 因此：粗分组取上游（otype 字段原样保留），
        「地方」则用 13 个地市各自的 assortId=120「本地」清单做交集剔除来定位。
      · 真 4K 由实测分辨率判定，单独成组并置顶。
    """
    province_ab = load_ability("320000")     # ⚠ 必须全省视角（320100 只覆盖 211 台）
    assorts = fetch_assortments(province_ab)
    log("      上游分组 %d 个: %s" % (len(assorts), "、".join(n for _i, n, _s in assorts)))

    # ① 分组归属表：uuid -> [上游分组名]
    #    用全省视角拉取：实测 320100 视角只有 211 个频道有分组归属，
    #    320000 全省视角有 266 个。用南京视角会让其余地市的 55 个台拿不到
    #    otype，被误判成「孤儿频道」（这就是曾出现 67 个假孤儿的原因）。
    member = {}
    for gid, gname, _s in assorts:
        try:
            for u in fetch_members(province_ab, gid):
                gl = member.setdefault(u, [])
                if gname not in gl:
                    gl.append(gname)
        except Exception as e:      # noqa: BLE001
            log("      分组 %s(%s) 拉取失败: %s" % (gname, gid, e))
    log("      分组归属 %d 个频道" % len(member))

    # ② 13 地市「本地」清单 → 定位真正的地方台（交集即省级台，剔除）
    local_lists, city_of = {}, {}
    for code, _cname in JIANGSU:
        try:
            local_lists[code] = fetch_members(load_ability(code), "120")
        except Exception:      # noqa: BLE001
            pass
    valid_local = [set(v) for v in local_lists.values() if v]
    common = set.intersection(*valid_local) if len(valid_local) > 1 else set()
    for code, uuids in local_lists.items():
        for u in uuids:
            if u not in common:
                city_of[u] = code
    log("      省级本地台（13 市共有）%d 个，地方台（交集法）%d 个"
        % (len(common), len(city_of)))

    # ②-b 上游 isLocal 全省视角（主判据，2026-11-25 起）
    #    与 13 市交集法并行：isLocal 负责「是不是县区级台」，city_of 负责「属哪个市」。
    islocal = {}
    try:
        islocal = fetch_islocal("320000")
    except Exception as e:      # noqa: BLE001
        log("      isLocal 拉取失败(%s)，回退纯交集法" % e)
    if islocal:
        cname_of = dict(islocal)
        only_local = set(islocal) - set(city_of)
        only_inter = set(city_of) - set(islocal)
        log("      isLocal 全省视角 %d 台；与交集法差异：isLocal 独有 %d / 交集法独有 %d"
            % (len(islocal), len(only_local), len(only_inter)))
        if only_local:
            log("        ·仅 isLocal 判定为地方台: %s"
                % "、".join(islocal[u] or u for u in list(only_local)[:8]))
        if only_inter:
            log("        ·仅交集法判定为地方台（isLocal 未标）: %s"
                % "、".join("%s(%s)" % (cname_of.get(u, u), DNAME.get(code, code))
                           for u, code in list((u, city_of[u]) for u in only_inter)[:8]))
        # 以 isLocal 为主判据并集（宁多勿漏：任一法认定是地方台就按地方台处理），
        # 这样上游新增县区台（isLocal=1 但 13 市清单尚未覆盖）不会漏成「江苏省级台」。
        city_of = {u: city_of.get(u, "") for u in (set(city_of) | set(islocal))}

    # ③ 频道主体：13 地市「全部」清单取并集（各地市清单并不相同）
    chs, seen, prs_list = [], set(), []
    for d in districts:
        ab = load_ability(d)
        sub = build_channels(ab, d, epg_enable=epg_enable)
        fresh = []
        for c in sub:
            if c["uuid"] in seen:
                continue
            seen.add(c["uuid"])
            fresh.append(c)
            chs.append(c)
        if fresh and epg_enable:
            prs_list.extend(build_programs(fresh, ab, hist_days, days, quiet=True))

    # ④ 归类：先记录上游原分组（供二级展示），地方台补 district，最后统一交 _classify
    for c in chs:
        gl = member.get(c["uuid"]) or []
        c["otype"] = gl[0] if gl else ""          # 上游原分组名（原样保留供界面二级展示）
        c["otypes"] = gl
        if c["uuid"] in city_of:
            c["district"] = city_of[c["uuid"]]
            c["group_from_city"] = True
        c["group"] = _classify(c)

    # ④-b 孤儿频道告警（上游未归任何分组的频道，只能靠名称兜底）
    _collect_orphans(chs)

    prs, pk = [], set()
    for p in sorted(prs_list, key=lambda v: v["start"]):
        k = (p["uuid"], p["start"])
        if k not in pk:
            pk.add(k)
            prs.append(p)
    gsum = {}
    for c in chs:
        gsum[c["group"]] = gsum.get(c["group"], 0) + 1
    log("      合计 %d 频道 / %d 条节目（%d 个地市）｜%s"
        % (len(chs), len(prs), len(districts),
           " ".join("%s%d" % (k, v) for k, v in sorted(gsum.items(),
                    key=lambda kv: GROUP_ORDER.index(kv[0]) if kv[0] in GROUP_ORDER else 9))))
    return chs, prs


def gkey(ch):
    """排序键：分组顺序 → 地方台按城市 → 组内台号 → 名字。

    4K 组特殊：要求「CCTV 台在最前，按数字升序，其余 4K 台排后面」。
    所以 4K 组内给一个细分序号：
      CCTV 类（CCTV-164K / CCTV1高清臻享 …）→ 0，按 CCTV 数字升序；
      央视4K                               → 1；
      其余（各卫视 4K 等）                  → 2，按台号。
    其余组保持「台号升序」，与运营商下发顺序一致。
    """
    g = ch["group"]
    rank = GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER)
    sub = DNAME.get(ch["district"], "") if g == "地方" else ""
    # sort_num 允许人工指定组内排序（如央视组补进来的 CCTV-1 要排在最前）
    num = int(ch.get("sort_num") or (re.sub(r"\D", "", ch["num"]) or 0))
    if g == GROUP_UHD:
        name = ch["name"]
        # 先剥掉「4K / 高清臻享 / UHD」等后缀再取 CCTV 台号，
        # 否则 CCTV-164K 会被贪婪匹配成 164（应为 16）。
        nm = re.sub(r"(4K|高清臻享|UHD|超高清)$", "", name, flags=re.I).strip()
        m = re.match(r"CCTV[- ]?(\d+)", nm, re.I)
        if m:                                   # CCTV 台：0 段，按 CCTV 数字升序
            return (rank, sub, 0, int(m.group(1)), name)
        if name.startswith("央视"):              # 央视4K：1 段
            return (rank, sub, 1, num, name)
        return (rank, sub, 2, num, name)        # 卫视 4K / 睛彩臻享 等：2 段
    return (rank, sub, num, ch["name"])


# ---------------------------------------------------------------- 输出生成

def write_outputs(out, cdn, channels, programs, backup_days, days, base_url="", minimal=False, epg_enable=True):
    os.makedirs(out, exist_ok=True)
    now = datetime.now(TZ8)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    nowts = int(time.time())
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    epg_lo = int((day0 - timedelta(days=5)).timestamp())
    epg_hi = int((day0 + timedelta(days=2)).timestamp())

    def write(name, text):
        tmp = os.path.join(out, name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, os.path.join(out, name))

    def live_of(ch):
        return "%s/%s/live1/%s/%s/1.m3u8" % (cdn, ch["cp"], ch["code"], ch["code"])

    def live_of_alt(ch, alt):
        """同频道的其它清晰度档地址（4K 播不动时切 1080P 用）。"""
        return "%s/%s/live1/%s/%s/1.m3u8" % (cdn, alt["cp"], alt["code"], alt["code"])

    # 默认占位符必须是 ${(b)fmt}：裸 {start} 任何播放器都不认（FongMi 实测解析成空串）。
    # /api/catchup 会自己用 strftime 生成时间，不依赖这里的占位符。
    def back_of(ch, va="${(b)yyyyMMddHHmmss}", vb="${(e)yyyyMMddHHmmss}"):
        return "%s/%s/lookback/%s/%s?playseek=%s-%s" % (cdn, ch["cp"], ch["code"], ch["code"], va, vb)

    def dlabel(ch):
        return DNAME.get(ch["district"], "") if ch["group"] == "地方" else "全省"

    def glabel(ch):
        return ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], "未定地市")

    fit = sorted(channels, key=gkey)
    base = (base_url or "").rstrip("/")
    tvg = (' url-tvg="%s/epg.xml" x-tvg-url="%s/epg.xml"' % (base, base)) if base and epg_enable else ""
    epg_links = {"epg": [base + "/epg.xml"], "epg_url": base + "/epg.xml"} if epg_enable else {}

    def logo_of(ch):
        """给播放器/EPG 用的台标地址：必须是绝对 URL。
        内存里的 ch["logo"] 是相对路径（供 Web 界面同源加载），
        写进 m3u / epg.xml 时必须转成绝对地址，否则电视端播放器取不到图。"""
        p = ch.get("logo") or ""
        if not p:
            return ""
        if p.startswith("http://") or p.startswith("https://"):
            return p
        return (base + p) if base else p

    # --- 预览用索引：channels.json（含当前/下档节目）
    programs_by_uuid = {}
    for p in programs:
        programs_by_uuid.setdefault(p["uuid"], []).append(p)
    now_idx = []
    for ch in fit:
        ps = programs_by_uuid.get(ch["uuid"], [])
        cur, nxt = None, None
        for p in ps:
            if p["start"] <= nowts < p["end"]:
                cur = p
            elif p["start"] >= nowts and nxt is None:
                nxt = p
        now_idx.append({
            "uuid": ch["uuid"], "name": ch["name"], "num": ch["num"],
            "group": glabel(ch), "district": dlabel(ch),
            "logo": ch["logo"], "logo_tone": ch.get("logo_tone", "light"), "back": ch["back"], "mc": ch["mc"],
            "live": live_of(ch),
            "catchup": back_of(ch) if ch["back"] else "",
            # ---- 4K / 多档：
            # is_4k    是不是真 4K（实测判定）
            # codec    视频编码 H.264 / H.265（H.265 在浏览器里播不了，界面要提示降档）
            # alts     该频道的其它清晰度档，4K 播不动时可直接切 1080P
            "is_4k": bool(ch.get("is_4k")),
            "uhd_unverified": bool(ch.get("uhd_unverified")),
            "grp_uhd": bool(ch.get("grp_uhd")),
            "zhx": bool(ch.get("zhx")),
            "w": ch.get("w"), "h": ch.get("h"),
            "codec": ch.get("codec") or "",
            "res_name": ch.get("res_name") or "",
            "otype": ch.get("otype") or "",
            "alts": [{"name": a["name"], "live": live_of_alt(ch, a)} for a in (ch.get("alts") or [])],
            "now": {"t": cur["title"], "s": cur["start"], "e": cur["end"]} if cur else None,
            "next": {"t": nxt["title"], "s": nxt["start"], "e": nxt["end"]} if nxt else None,
        })
    write("channels.json", json.dumps(now_idx, ensure_ascii=False))
    # 节目全量表（UI 点开某频道时按需取）
    write("programs.json", json.dumps(programs_by_uuid, ensure_ascii=False))

    # ---- 精简模式：TVBox 三件套 + FongMi 三件套 + Kodi ----
    if minimal:
        # 精简模式也保留一份 Kodi 专用文件（回看变量语法不同），
        # 否则界面上的「Kodi 接入」标签会指向不存在的文件。

        # 默认 default 模式：本源的「回看」是独立路径 /gitv/lookback/，
        # 与直播路径 /gitv/live1/ 不同段，append（往直播URL追加参数）根本拼不出回看地址。
        # 只有 default 才把 catchup-source 当完整回看 URL 用 —— 这是唯一正确模式。
        def build_m3u(va, vb, catchup_mode="default"):
            lines = ["#EXTM3U" + tvg,
                     "# 江苏移动 IPTV｜更新: %s｜节目单: 前5天~明天｜%d 频道" % (stamp, len(channels))]
            for ch in fit:
                catch = ""
                if ch["back"] > 0:
                    catch = (' catchup="%s" catchup-days="%d" catchup-source="%s"'
                             % (catchup_mode, backup_days, back_of(ch, va, vb)))
                lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-chno="%s" tvg-logo="%s" group-title="%s"%s,%s'
                             % (ch["uuid"], ch["name"], ch["num"], logo_of(ch), glabel(ch), catch, ch["name"]))
                lines.append(live_of(ch))
            return "\n".join(lines)

        # 回看占位符按播放器语法分套产出（依据各自源码实测，不是猜的）：
        #   ${(b)fmt}   FongMi(Catchup.java) / APTV(docs) / mytv-android(Release) / TVBox / 酷9
        #   ${start:fmt} Kodi(pvr.iptvsimple CatchupController.cpp)
        # 裸 {start} 谁都不认；${start} 无格式参数时 Kodi 会填整数时间戳，源站也不认。
        ap = ("${(b)yyyyMMddHHmmss}", "${(e)yyyyMMddHHmmss}")
        body = build_m3u(*ap)
        write("iptv.m3u8", body)      # 主入口，绝大多数播放器用这个
        write("fongmi.m3u", body)     # FongMi 面板固定读这个文件名，内容与主入口一致
        write("kodi.m3u", build_m3u("${start:YmdHMS}", "${end:YmdHMS}"))

        x = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="jsydiptv">']
        for ch in channels:
            icon = '<icon src="%s"/>' % _xml_attr(logo_of(ch)) if ch.get("logo") else ""
            x.append('  <channel id="%s"><display-name>%s</display-name>%s</channel>'
                     % (_xml_attr(ch["uuid"]), escape(ch["name"]), icon))
        for p in programs:
            if p["end"] <= epg_lo or p["start"] >= epg_hi:
                continue
            s = datetime.fromtimestamp(p["start"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
            e = datetime.fromtimestamp(p["end"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
            x.append('  <programme start="%s" stop="%s" channel="%s"><title lang="zh">%s</title></programme>'
                     % (s, e, _xml_attr(p["uuid"]), escape(p["title"])))
        x.append("</tv>")
        write("epg.xml", "\n".join(x))

        write("tvbox.json", json.dumps({
            "lives": [{"name": "江苏移动IPTV", "type": 0,
                       "url": base + "/iptv.m3u8", "playerType": 1}],
            **epg_links,
        }, ensure_ascii=False, indent=2))

        write_fongmi_outputs(out, base, channels, programs, backup_days, day0, days, epg_enable)
        return

    # ---- 完整模式 ----
    for _s, fname, va, vb in STYLES:
        lines = ["#EXTM3U" + tvg,
                 "# 江苏移动 IPTV 直播源｜更新: %s｜节目单: 前5天~明天" % stamp,
                 "# 全江苏 %d 频道｜回看 %d 个" % (len(channels), sum(1 for c in channels if c["back"] > 0))]
        for ch in fit:
            catch = ""
            if ch["back"] > 0:
                catch = (' catchup="default" catchup-days="%d" catchup-source="%s"'
                         % (backup_days, back_of(ch, va, vb)))
            lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-chno="%s" tvg-logo="%s" group-title="%s"%s,%s'
                         % (ch["uuid"], ch["name"], ch["num"], logo_of(ch), glabel(ch), catch, ch["name"]))
            lines.append(live_of(ch))
        write(fname, "\n".join(lines))

    # 固定入口
    for alias, src in {"iptv.m3u8": "直播源.m3u", "iptv_fongmi.m3u8": "直播源_tvbox_fongmi.m3u"}.items():
        shutil.copyfile(os.path.join(out, src), os.path.join(out, alias))

    write_fongmi_outputs(out, base, channels, programs, backup_days, day0, days, epg_enable)

    txt, cur = [], None
    for ch in fit:
        g = glabel(ch)
        if g != cur:
            cur = g
            txt.append("%s,#genre#" % g)
        txt.append("%s,%s" % (ch["name"], live_of(ch)))
    write("直播源.txt", "\n".join(txt) + "\n")

    groups = {}
    for ch in fit:
        groups.setdefault(glabel(ch), []).append({"name": ch["name"], "urls": [live_of(ch)]})
    write("直播源.json", json.dumps([{"group": g, "channels": v} for g, v in groups.items()],
                                   ensure_ascii=False, indent=1))

    mm = ["#EXTM3U"]
    for ch in fit:
        if "udp://" in (ch["mc"] or ""):
            mm.append('#EXTINF:-1 tvg-chno="%s" group-title="%s",%s' % (ch["num"], ch["group"], ch["name"]))
            mm.append(ch["mc"])
    write("组播源.m3u", "\n".join(mm) + "\n")

    x = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="jsydiptv">']
    for ch in channels:
        icon = '<icon src="%s"/>' % _xml_attr(logo_of(ch)) if ch.get("logo") else ""
        x.append('  <channel id="%s"><display-name>%s</display-name>%s</channel>'
                 % (_xml_attr(ch["uuid"]), escape(ch["name"]), icon))
    for p in programs:
        if p["end"] <= epg_lo or p["start"] >= epg_hi:
            continue
        s = datetime.fromtimestamp(p["start"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
        e = datetime.fromtimestamp(p["end"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
        x.append('  <programme start="%s" stop="%s" channel="%s"><title lang="zh">%s</title></programme>'
                 % (s, e, _xml_attr(p["uuid"]), escape(p["title"])))
    x.append("</tv>")
    write("epg.xml", "\n".join(x))
    shutil.copyfile(os.path.join(out, "epg.xml"), os.path.join(out, "EPG.xml"))

    with open(os.path.join(out, "频道清单.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["num", "name", "group", "district", "uuid", "code", "cp", "back", "mc"])
        w.writeheader()
        for ch in fit:
            w.writerow({"num": ch["num"], "name": ch["name"], "group": ch["group"],
                        "district": dlabel(ch), "uuid": ch["uuid"],
                        "code": ch["code"], "cp": ch["cp"], "back": ch["back"], "mc": ch["mc"]})
    os.replace(os.path.join(out, "频道清单.csv.tmp"), os.path.join(out, "频道清单.csv"))

    pc = programs_by_uuid
    with open(os.path.join(out, "节目源对照.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["分组", "频道名", "地区", "流代码", "CP", "支持回看", "节目数",
                    "直播地址", "回看地址模板(替换start-end)"])
        for ch in fit:
            ps = pc.get(ch["uuid"], [])
            w.writerow([glabel(ch), ch["name"], dlabel(ch), ch["code"], ch["cp"],
                        "是" if ch["back"] else "否", len(ps), live_of(ch),
                        back_of(ch) if ch["back"] else ""])
    os.replace(os.path.join(out, "节目源对照.csv.tmp"), os.path.join(out, "节目源对照.csv"))

    by_box = sorted(channels, key=lambda c: (int(re.sub(r"\D", "", c["num"]) or 0), c["name"]))
    past_by = {}
    for p in programs:
        if p["end"] < nowts and p["start"] >= nowts - backup_days * 86400:
            past_by.setdefault(p["uuid"], []).append(p)
    with open(os.path.join(out, "频道对照_机顶盒顺序.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["机顶盒序号", "频道名", "分组", "地区", "直播源", "回看模板", "可回看节目数"])
        for ch in by_box:
            w.writerow([ch["num"], ch["name"], glabel(ch), dlabel(ch), live_of(ch),
                        back_of(ch) if ch["back"] else "",
                        len(past_by.get(ch["uuid"], [])) if ch["back"] else 0])
    os.replace(os.path.join(out, "频道对照_机顶盒顺序.csv.tmp"),
               os.path.join(out, "频道对照_机顶盒顺序.csv"))

    with open(os.path.join(out, "回看节目单.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["机顶盒序号", "频道名", "节目名", "开始", "结束", "回看地址(可直接播)"])
        for ch in by_box:
            if not ch["back"]:
                continue
            for p in sorted(past_by.get(ch["uuid"], []), key=lambda v: -v["start"]):
                w.writerow([ch["num"], ch["name"], p["title"],
                            datetime.fromtimestamp(p["start"], TZ8).strftime("%Y-%m-%d %H:%M"),
                            datetime.fromtimestamp(p["end"], TZ8).strftime("%H:%M"),
                            "%s/%s/lookback/%s/%s?playseek=%s-%s" % (
                                cdn, ch["cp"], ch["code"], ch["code"],
                                datetime.fromtimestamp(p["start"], TZ8).strftime("%Y%m%d%H%M%S"),
                                datetime.fromtimestamp(p["end"], TZ8).strftime("%Y%m%d%H%M%S"))])
    os.replace(os.path.join(out, "回看节目单.csv.tmp"), os.path.join(out, "回看节目单.csv"))

    # 此处全部中文源文件已落盘，再生成固定的 ASCII 入口。
    for alias, src in ALIASES.items():
        shutil.copyfile(os.path.join(out, src), os.path.join(out, alias))

    write("tvbox.json", json.dumps({
        "lives": [
            {"name": "江苏移动·回看版", "type": 0, "url": base + "/iptv.m3u8", "playerType": 1},
            {"name": "江苏移动·纯直播", "type": 1, "url": base + "/live.txt", "playerType": 1},
        ],
        **epg_links,
    }, ensure_ascii=False, indent=2))


def write_fongmi_outputs(out, base, channels, programs, backup_days, day0, days, epg_enable):
    """两种模式共享。预生成今天及配置范围内的未来日期，避免午夜到更新之间断档。"""
    live = {"group": "江苏移动IPTV", "name": "智能分组", "type": 0,
            "url": base + "/fongmi.m3u", "playerType": 1, "ua": STREAM_UA}
    # 三套订阅并存：同一个 FongMi 配置里直接看到三套，不用填三个地址。
    # lives[0] 保持「智能分组」，兼容已按旧结构配置的用户。
    others = [{"group": "江苏移动IPTV", "name": "原版分组", "type": 0,
               "url": base + "/orig.m3u8", "playerType": 1, "ua": STREAM_UA},
              {"group": "江苏移动IPTV", "name": "自定义分组", "type": 0,
               "url": base + "/mine_fongmi.m3u", "playerType": 1, "ua": STREAM_UA}]
    config = {"notice": "江苏移动IPTV · FongMi（%d 频道 / 回看 %d 天）" % (len(channels), backup_days),
              "sites": [], "lives": [live] + others}
    if epg_enable:
        live["epg"] = base + "/epg/{id}/{date}.json"
        config["epg"] = [live["epg"]]
    with open(os.path.join(out, "fongmi.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    if not epg_enable:
        return
    lo = day0.date()
    hi = lo + timedelta(days=days)
    buckets = {}
    for pg in programs:
        uid = str(pg["uuid"])
        if not _valid_epg_uid(uid) or pg["end"] <= pg["start"]:
            continue
        st = datetime.fromtimestamp(pg["start"], TZ8)
        en = datetime.fromtimestamp(pg["end"], TZ8)
        d = max(lo, st.date())
        last = min(hi, datetime.fromtimestamp(pg["end"] - 1, TZ8).date())
        while d <= last:
            buckets.setdefault((uid, d.isoformat()), set()).add((st, en, pg["title"]))
            d += timedelta(days=1)
    for (uid, day), items in buckets.items():
        folder = os.path.join(out, "epg", uid)
        os.makedirs(folder, exist_ok=True)
        entries = [{"start": st.strftime("%H:%M"), "end": en.strftime("%H:%M"), "title": title}
                   for st, en, title in sorted(items)]
        with open(os.path.join(folder, day + ".json"), "w", encoding="utf-8") as f:
            json.dump({"date": day, "epg_data": entries}, f, ensure_ascii=False, separators=(",", ":"))


def _valid_epg_uid(uid):
    return uid not in (".", "..") and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", uid) is not None


# ---------------------------------------------------------------- 方案二：上游原版清单
#
# 与主产物完全独立、互不影响：
#   · 主产物（iptv.m3u8 等）走本应用的二次分类（4K/央视/卫视/江苏/地方/6 内容组）；
#   · 原版产物（orig.m3u8 / orig_epg.xml）使用上游 11 组及归属重建；
#     台名、台号与排序沿用本应用当前数据。
#
# 为什么单独出一套而不是替换：「本地」组把江苏省级台和 13 地市县区台混在 67 台里，
# 「专业频道」19 台内容完全杂糅，「超高清」里还混着实测 1080P 的高清臻享 ——
# 直接替换会让这三处退回问题状态。因此保留两套，用户按需选。

def write_orig_outputs(out, cdn, channels, programs, backup_days, base_url=""):
    """生成「江苏移动原版节目清单」：orig.m3u8 + orig_epg.xml + orig.json。

    分组 = 上游 11 组原样（ORIG_GROUPS），一个频道可同时出现在多个组
    （电视墙本身就这样展示：CCTV-5 同时在央视与体育组）。
    """
    base = (base_url or "").rstrip("/")
    now = datetime.now(TZ8)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    epg_lo = int((day0 - timedelta(days=5)).timestamp())
    epg_hi = int((day0 + timedelta(days=2)).timestamp())
    nowts = int(time.time())

    def write(name, text):
        tmp = os.path.join(out, name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, os.path.join(out, name))

    def live_of(ch):
        return "%s/%s/live1/%s/%s/1.m3u8" % (cdn, ch["cp"], ch["code"], ch["code"])

    # 与 write_outputs 里的同名函数重复定义，默认值必须同为 ${(b)fmt}，
    # 否则 orig.m3u8 会退回裸 {start} 语法（任何播放器都不认）。
    def back_of(ch, va="${(b)yyyyMMddHHmmss}", vb="${(e)yyyyMMddHHmmss}"):
        return "%s/%s/lookback/%s/%s?playseek=%s-%s" % (
            cdn, ch["cp"], ch["code"], ch["code"], va, vb)

    def logo_of(ch):
        p = ch.get("logo") or ""
        if not p:
            return ""
        if p.startswith("http://") or p.startswith("https://"):
            return p
        return (base + p) if base else p

    def oname(ch):
        """台名直接用当前名称（不做上游原名还原）。"""
        return ch["name"]

    def onum(ch):
        return ch["num"]

    # 上游原分组名 → 该组下的频道（按台号升序，与电视墙顺序接近）
    member = {c["uuid"]: (c.get("otypes") or []) for c in channels}
    grouped = {}
    for _aid, gname in ORIG_GROUPS:
        hit = [c for c in channels if gname in member.get(c["uuid"], [])]
        if gname == "超高清":
            hit, _ = _unique_uhd(hit)
        hit.sort(key=lambda x: (int(re.sub(r"\D", "", onum(x) or "") or 0), oname(x)))
        grouped[gname] = hit

    lines = ["#EXTM3U",
             "# 江苏移动 IPTV · 原版分组清单（按机顶盒电视墙分组重建）｜更新: %s" % stamp,
             "# 分组及归属取自上游；台名、台号和排序使用当前频道数据",
             "# 同一频道可能出现在多个分组（与电视墙一致）"]
    total = 0
    for gname, chs_g in grouped.items():
        for ch in chs_g:
            catch = ""
            if ch.get("back", 0) > 0:
                # 占位符用 back_of 的默认值（${(b)fmt}），不再逐个传参，避免两处不同步
                catch = (' catchup="default" catchup-days="%d" catchup-source="%s"'
                         % (backup_days, back_of(ch)))
            lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-chno="%s" '
                         'tvg-logo="%s" group-title="%s"%s,%s'
                         % (ch["uuid"], oname(ch), onum(ch), logo_of(ch), gname, catch, oname(ch)))
            lines.append(live_of(ch))
            total += 1
    write("orig.m3u8", "\n".join(lines))

    # 原版 EPG：频道 id 用 uuid（与 m3u 的 tvg-id 对齐），只含出现在原版清单里的频道
    used = {c["uuid"] for g in grouped.values() for c in g}
    x = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="jsydiptv-orig">']
    for ch in [c for c in channels if c["uuid"] in used]:
        icon = '<icon src="%s"/>' % _xml_attr(logo_of(ch)) if ch.get("logo") else ""
        x.append('  <channel id="%s"><display-name>%s</display-name>%s</channel>'
                 % (_xml_attr(ch["uuid"]), escape(oname(ch)), icon))
    for p in programs:
        if p["uuid"] not in used or p["end"] <= epg_lo or p["start"] >= epg_hi:
            continue
        s = datetime.fromtimestamp(p["start"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
        e = datetime.fromtimestamp(p["end"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
        x.append('  <programme start="%s" stop="%s" channel="%s"><title lang="zh">%s</title></programme>'
                 % (s, e, _xml_attr(p["uuid"]), escape(p["title"])))
    x.append("</tv>")
    write("orig_epg.xml", "\n".join(x))

    # 机器可读清单（界面上展示「原版分组与频道」用）
    programs_by_uuid = {}
    for p in programs:
        programs_by_uuid.setdefault(p["uuid"], []).append(p)

    def ch_obj(ch):
        ps = programs_by_uuid.get(ch["uuid"], [])
        cur = nxt = None
        for p in ps:
            if p["start"] <= nowts < p["end"]:
                cur = p
            elif p["start"] >= nowts and nxt is None:
                nxt = p
        return {"num": onum(ch), "name": oname(ch), "uuid": ch["uuid"],
                "logo": ch.get("logo") or "", "back": ch.get("back", 0),
                "live": live_of(ch),
                "now": cur["title"] if cur else "",
                "next": nxt["title"] if nxt else ""}

    write("orig.json", json.dumps({
        "updated": stamp,
        "note": "使用上游 11 组及归属重建；超高清组同台优先保留实测 4K",
        "total_entries": total,
        "unique_channels": len(used),
        "groups": [{"name": gname, "assortId": aid, "count": len(grouped[gname]),
                    "channels": [ch_obj(c) for c in grouped[gname]]}
                   for aid, gname in ORIG_GROUPS],
    }, ensure_ascii=False, indent=2))
    return total, len(used)


# ---------------------------------------------------------------- 第三套：自定义分组
#
# 三套订阅并存、互不影响：
#   ① 智能分组  iptv.m3u8 / kodi.m3u / fongmi.m3u   —— _classify() 判定
#   ② 原版分组  orig.m3u8 / orig_epg.xml / orig.json —— 上游 11 组原样
#   ③ 自定义    mine.m3u8 / mine_kodi.m3u / mine_fongmi.m3u / mine.json
#
# 第三套只做「展示层」：改分组名、搬频道、调顺序、隐藏。
# 频道地址、回看模板、节目单完全复用，EPG 三套共用一份 epg.xml。

LAYOUT_FILE_NAME = "layout.json"
MINE_GROUP_LIMIT = 50
MINE_TITLE_LIMIT = 24
MINE_RULE_LIMIT = 500
MINE_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MINE_RULE_TYPES = ("channel", "orig_group", "match", "flag")
MINE_FLAGS = ("is_4k", "back", "zhx")


def _layout_path():
    return os.path.join(PKGETC, LAYOUT_FILE_NAME)


def seed_layout(original):
    """首次种子：按上游 11 组生成，规则是「属于上游组 X」。

    用规则而不是 uuid 列表，运营商改动分组后自定义套自动跟随。
    """
    groups = []
    for i, (_aid, gname) in enumerate(ORIG_GROUPS):
        groups.append({"id": "g%d" % (i + 1), "title": gname,
                       "rules": [{"type": "orig_group", "value": gname}]})
    return {"version": 1, "seeded_from": "orig", "groups": groups,
            "order": [], "hide": [], "renumber": False}


def _layout_valid_rule(r):
    if not isinstance(r, dict):
        return False
    t = r.get("type")
    if t not in MINE_RULE_TYPES:
        return False
    if t == "channel":
        return isinstance(r.get("uuid"), str) and 0 < len(r["uuid"]) <= 64
    if t == "orig_group":
        return isinstance(r.get("value"), str) and 0 < len(r["value"]) <= 32
    if t == "flag":
        return r.get("flag") in MINE_FLAGS
    field, op, val = r.get("field"), r.get("op"), r.get("value")
    if field not in ("name", "num") or op not in ("contains", "equals", "regex"):
        return False
    if not isinstance(val, str) or not val or len(val) > 64:
        return False
    if op == "regex":
        try:
            re.compile(val)
        except re.error:
            return False
    return True


def validate_layout(layout):
    """返回 (规范化后的布局, 错误信息)。错误信息为空表示通过。"""
    if not isinstance(layout, dict):
        return None, "布局必须是 JSON 对象"
    groups = layout.get("groups")
    if not isinstance(groups, list):
        return None, "groups 必须是数组"
    if len(groups) > MINE_GROUP_LIMIT:
        return None, "分组不能超过 %d 个" % MINE_GROUP_LIMIT
    seen, total_rules, out_groups = set(), 0, []
    for g in groups:
        if not isinstance(g, dict):
            return None, "分组项必须是对象"
        gid = g.get("id")
        if not isinstance(gid, str) or not MINE_GROUP_ID_RE.match(gid):
            return None, "分组 id 只能是字母数字下划线短横线（1-32 位）"
        if gid in seen:
            return None, "分组 id 重复：%s" % gid
        seen.add(gid)
        title = g.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, "分组标题不能为空"
        title = title.strip()
        if len(title) > MINE_TITLE_LIMIT:
            return None, "分组标题不能超过 %d 个字符：%s" % (MINE_TITLE_LIMIT, title)
        rules = g.get("rules") or []
        if not isinstance(rules, list):
            return None, "rules 必须是数组"
        for r in rules:
            if not _layout_valid_rule(r):
                return None, "分组「%s」里有无效规则" % title
        total_rules += len(rules)
        out_groups.append({"id": gid, "title": title, "rules": rules,
                           "multi": bool(g.get("multi"))})
    if total_rules > MINE_RULE_LIMIT:
        return None, "规则总数不能超过 %d 条" % MINE_RULE_LIMIT
    hide = layout.get("hide") or []
    if not isinstance(hide, list):
        return None, "hide 必须是数组"
    for r in hide:
        if not _layout_valid_rule(r):
            return None, "隐藏列表里有无效规则"
    order = layout.get("order") or []
    if not isinstance(order, list):
        return None, "order 必须是数组"
    for o in order:
        if not isinstance(o, dict) or not _layout_valid_rule({k: v for k, v in o.items() if k != "pos"}):
            return None, "排序项里有无效规则"
        if not isinstance(o.get("pos"), int) or o["pos"] < 1:
            return None, "排序位次必须是正整数"
    return {"version": 1, "seeded_from": layout.get("seeded_from") or "orig",
            "groups": out_groups, "hide": hide, "order": order,
            "renumber": bool(layout.get("renumber"))}, ""


def load_layout():
    try:
        with open(_layout_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_layout(layout):
    os.makedirs(PKGETC, exist_ok=True)
    tmp = _layout_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _layout_path())


def _mine_match(ch, rule, member):
    t = rule.get("type")
    if t == "channel":
        return ch["uuid"] == rule.get("uuid")
    if t == "orig_group":
        return rule.get("value") in (member.get(ch["uuid"]) or [])
    if t == "flag":
        f = rule.get("flag")
        if f == "is_4k":
            return bool(ch.get("is_4k"))
        if f == "back":
            return ch.get("back", 0) > 0
        return bool(ch.get("zhx"))
    field, op, val = rule.get("field"), rule.get("op"), rule.get("value")
    src = ch.get("name", "") if field == "name" else str(ch.get("num", ""))
    if op == "contains":
        return val in src
    if op == "equals":
        return src == val
    try:
        return re.search(val, src) is not None
    except re.error:
        return False


def apply_mine_layout(channels, layout, member):
    """求值得到 [(频道, 分组标题), ...]，顺序即输出顺序。

    · 分组顺序 = groups 数组顺序，未命中的频道落到末尾「其他」。
    · 一台可以多组：只有标了 multi 的分组会额外收录，且必须显式打开，
      避免出现用户没预期的重复频道。
    · 隐藏优先于分组；全被隐藏时返回空，由调用方回退。
    """
    groups = layout.get("groups") or []
    hide = layout.get("hide") or []
    order = layout.get("order") or []
    pos_of = {}
    for o in order:
        for ch in channels:
            if _mine_match(ch, {k: v for k, v in o.items() if k != "pos"}, member):
                pos_of.setdefault(ch["uuid"], o["pos"])
    kept = [ch for ch in channels
            if not any(_mine_match(ch, r, member) for r in hide)]
    buckets = [[] for _ in groups]
    ungrouped = []
    for ch in kept:
        hits = [i for i, g in enumerate(groups) if any(_mine_match(ch, r, member) for r in g["rules"])]
        if not hits:
            ungrouped.append(ch)
            continue
        buckets[hits[0]].append(ch)
        for i in hits[1:]:
            if groups[i].get("multi"):
                buckets[i].append(ch)
    big = 10 ** 9

    def num_of(ch):
        return int(re.sub(r"\D", "", str(ch.get("num", ""))) or 0)

    for b in buckets:
        b.sort(key=lambda ch: (pos_of.get(ch["uuid"], big), num_of(ch), ch.get("name", "")))
    ungrouped.sort(key=lambda ch: (num_of(ch), ch.get("name", "")))
    entries, stats = [], {"groups": 0, "entries": 0, "channels": 0, "multi": 0}
    for g, b in zip(groups, buckets):
        if not b:
            continue                      # 空分组不进订阅，播放器里不留空目录
        stats["groups"] += 1
        for ch in b:
            entries.append((ch, g["title"]))
    if ungrouped:
        stats["groups"] += 1
        for ch in ungrouped:
            entries.append((ch, "其他"))
    stats["entries"] = len(entries)
    stats["channels"] = len({ch["uuid"] for ch, _t in entries})
    stats["multi"] = stats["entries"] - stats["channels"]
    stats["hidden"] = len(channels) - len(kept)
    stats["ungrouped"] = len(ungrouped)
    return entries, stats


def write_mine_outputs(out, cdn, entries, backup_days, base_url, epg_enable, layout):
    """第三套产物：三种回看语法 + JSON 索引。EPG 复用 epg.xml。"""
    os.makedirs(out, exist_ok=True)
    base = (base_url or "").rstrip("/")
    stamp = datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")
    tvg = (' url-tvg="%s/epg.xml" x-tvg-url="%s/epg.xml"' % (base, base)) if base and epg_enable else ""
    renumber = bool(layout.get("renumber"))
    chno = {}
    if renumber:
        for i, (ch, _t) in enumerate(entries, 1):
            chno.setdefault(ch["uuid"], i)

    def logo_of(ch):
        p = ch.get("logo") or ""
        if not p or p.startswith(("http://", "https://")):
            return p
        return (base + p) if base else p

    def live_of(ch):
        return "%s/%s/live1/%s/%s/1.m3u8" % (cdn, ch["cp"], ch["code"], ch["code"])

    def back_of(ch, va, vb):
        return "%s/%s/lookback/%s/%s?playseek=%s-%s" % (
            cdn, ch["cp"], ch["code"], ch["code"], va, vb)

    def write(name, text):
        tmp = os.path.join(out, name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, os.path.join(out, name))

    def build(va, vb):
        lines = ["#EXTM3U" + tvg,
                 "# 江苏移动 IPTV · 自定义分组｜更新: %s｜%d 条" % (stamp, len(entries))]
        for ch, group in entries:
            catch = ""
            if ch.get("back", 0) > 0:
                catch = (' catchup="default" catchup-days="%d" catchup-source="%s"'
                         % (backup_days, back_of(ch, va, vb)))
            num = chno.get(ch["uuid"], ch["num"]) if renumber else ch["num"]
            lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-chno="%s" tvg-logo="%s" group-title="%s"%s,%s'
                         % (ch["uuid"], ch["name"], num, logo_of(ch), group, catch, ch["name"]))
            lines.append(live_of(ch))
        return "\n".join(lines)

    body = build("${(b)yyyyMMddHHmmss}", "${(e)yyyyMMddHHmmss}")
    write("mine.m3u8", body)
    write("mine_fongmi.m3u", body)
    write("mine_kodi.m3u", build("${start:YmdHMS}", "${end:YmdHMS}"))

    grouped = []
    for ch, group in entries:
        if not grouped or grouped[-1]["name"] != group:
            grouped.append({"name": group, "channels": []})
        grouped[-1]["channels"].append({
            "num": chno.get(ch["uuid"], ch["num"]) if renumber else ch["num"],
            "name": ch["name"], "uuid": ch["uuid"],
            "logo": logo_of(ch), "back": ch.get("back", 0), "live": live_of(ch)})
    write("mine.json", json.dumps({
        "updated": stamp, "note": "用户自定义分组与顺序；节目单复用 epg.xml",
        "total_entries": len(entries), "groups": grouped}, ensure_ascii=False, indent=2))
    return len(entries), len({ch["uuid"] for ch, _t in entries})


# ---------------------------------------------------------------- 自检与发布

def probe_live(url, timeout=8):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.status == 200 and "#EXTM3U" in r.read(300).decode("utf-8", "ignore")
    except Exception:
        return False


def validate_live(channels, cdn):
    rnd = random.Random(2026)
    picks, seen = [], {}
    for ch in channels:
        k = ch["group"]
        if seen.get(k, 0) < 4:
            seen[k] = seen.get(k, 0) + 1
            picks.append(ch)
    sampled = {ch["uuid"] for ch in picks}
    remaining = [ch for ch in channels if ch["uuid"] not in sampled]
    sample = picks + rnd.sample(remaining, min(10, len(remaining)))

    def one(ch):
        return probe_live("%s/%s/live1/%s/%s/1.m3u8" % (cdn, ch["cp"], ch["code"], ch["code"]))

    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        live_ok = sum(1 for r in ex.map(one, sample) if r)

    detail = {"live": "%d/%d" % (live_ok, len(sample)), "sample": len(sample)}
    return live_ok >= max(1, (len(sample) * 8 + 9) // 10), detail


_CORE_OUTPUTS = {"iptv.m3u8", "kodi.m3u", "fongmi.m3u", "fongmi.json", "epg.xml", "tvbox.json",
                 "meta.json", "4k.json", "channels.json", "programs.json", "orig.m3u8", "orig_epg.xml", "orig.json",
                 "mine.m3u8", "mine_kodi.m3u", "mine_fongmi.m3u", "mine.json"}


def validate_generation(build, minimal, epg_enable):
    """校验整代产物，含元数据和嵌套节目单。不将命中的内容写回日志。"""
    required = _CORE_OUTPUTS if minimal else _CORE_OUTPUTS | {"直播源.txt", "直播源.m3u", "频道清单.csv"}
    for name in sorted(required):
        if not os.path.isfile(os.path.join(build, name)):
            raise RuntimeError("产物不完整: " + name)
    for name in ("iptv.m3u8", "kodi.m3u", "fongmi.m3u", "orig.m3u8"):
        with open(os.path.join(build, name), encoding="utf-8") as f:
            if not f.readline().startswith("#EXTM3U") or not any(line.startswith("#EXTINF:") for line in f):
                raise RuntimeError("直播清单没有有效频道: " + name)
    for name in ("epg.xml", "orig_epg.xml"):
        xml = ET.parse(os.path.join(build, name)).getroot()
        if xml.tag != "tv" or (not epg_enable and xml.find("programme") is not None):
            raise RuntimeError("XMLTV 与 EPG 配置不符: " + name)
    if epg_enable and (_count_epg(build) == 0 or not os.path.isdir(os.path.join(build, "epg"))):
        raise RuntimeError("EPG 已启用，但没有可用节目数据")
    for folder, _, files in os.walk(build):
        for fn in files:
            if not fn.endswith((".m3u", ".m3u8", ".txt", ".json", ".csv", ".md", ".xml")):
                continue
            path = os.path.join(folder, fn)
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
            if fn.endswith(".json"):
                json.loads(text)
            for pat, name in PRIVACY_PATTERNS:
                if re.search(pat, text):
                    raise RuntimeError("隐私检查未通过: %s (%s)" % (os.path.relpath(path, build), name))


def publish(build):
    """仅一次原子替换对读者可见；旧路径与上一代不会被此操作覆盖。"""
    link = build + ".link"
    try:
        os.symlink(os.path.relpath(build, DATA), link)
        os.replace(link, os.path.join(DATA, ".current"))
    finally:
        if os.path.islink(link):
            os.unlink(link)


def _prune_generations(root, protected):
    # 给已绑定旧代的 HTTP 请求留足窗口；上一次成功产物始终保留。
    cutoff = time.time() - 7 * 86400
    try:
        for entry in os.scandir(root):
            if (entry.name.startswith("gen-") and entry.path not in protected
                    and entry.is_dir(follow_symlinks=False) and entry.stat().st_mtime < cutoff):
                shutil.rmtree(entry.path)
    except OSError as e:
        log("[维护] 历史产物清理未完成:", e)


def _count_epg(build):
    """统计 epg.xml 里真正写出的 programme 条数。
    采集到的节目（meta.programs）包含回看历史区间，与播放器实际读到的条数不同，
    界面必须展示后者，否则用户会拿它跟播放器里看到的数量对不上。"""
    p = os.path.join(build, "epg.xml")
    try:
        with open(p, encoding="utf-8", errors="ignore") as f:
            return sum(1 for line in f if line.lstrip().startswith("<programme "))
    except Exception:
        return 0


def run_once(cfg, base_url=""):
    districts = [c for c, _ in JIANGSU] if str(cfg["district"]).lower() == "all" \
        else [x.strip() for x in str(cfg["district"]).split(",") if x.strip()]
    root = os.path.join(DATA, ".generations")
    os.makedirs(root, exist_ok=True)
    epg_enable = cfg.get("epg_enable", True)
    cache = os.path.join(PKGVAR, "cdn_host.txt")
    last = None
    # 同一数据目录不允许后台服务和 CLI --once 同时更新。
    with open(os.path.join(DATA, ".update.lock"), "a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("此数据目录已有更新在进行") from None
        previous = _output_dir()
        chs, prs = collect(districts, cfg["days"], cfg["history_days"], epg_enable=epg_enable)
        if not chs:
            raise RuntimeError("一个频道都没拿到，保留上一版")
        for h in ordered_hosts(cache):
            cdn = "http://" + h
            ok, detail = validate_live(chs, cdn)
            log("[3/5] CDN %s 直播清单抽检 %s" % (h, detail["live"]))
            if not ok:
                last = detail
                continue
            build = tempfile.mkdtemp(prefix="gen-", dir=root)
            published = False
            try:
                meta = _prepare_generation(build, previous, cfg, base_url, cdn, chs, prs, detail, epg_enable)
                publish(build)
                published = True
            finally:
                if not published and _output_dir() != os.path.realpath(build):
                    shutil.rmtree(build, ignore_errors=True)
            try:
                with open(cache, "w", encoding="utf-8") as f:
                    f.write(h)
            except OSError:
                pass
            _prune_generations(root, {previous, build})
            log("[5/5] 发布完成 -> %s（%s）" % (meta["generation"], detail))
            return meta
    raise RuntimeError("所有 CDN 均未通过自检（最后: %s），保留上一版" % last)


def _guard_epg_coverage(previous, channels, programs):
    """Compare like-for-like channel coverage; don't assume all channels carry a guide."""
    try:
        with open(os.path.join(previous, "programs.json"), encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(existing, dict):
        return
    baseline = {str(c["uuid"]) for c in channels if existing.get(str(c["uuid"]))}
    actual = {str(row["uuid"]) for row in programs}
    covered = len(baseline & actual)
    if len(baseline) >= 10 and covered * 5 < len(baseline) * 3:
        raise RuntimeError("节目单覆盖下降（原有 %d 台仅剩 %d 台），保留上一版" % (len(baseline), covered))


def _prepare_generation(build, previous, cfg, base_url, cdn, chs, prs, detail, epg_enable):
    all_ids = {c["uuid"] for c in chs}
    prs = [p for p in prs if p["uuid"] in all_ids] if epg_enable else []
    if cfg["localize_logo"]:
        n = localize_logos(chs, base_url, os.path.join(build, "logo"), os.path.join(previous, "logo"))
        log("[台标] 本地化 %d/%d" % (n, len(chs)))
    log("[3/5] 实测高画质频道分辨率 ...")
    probe_uhd(chs, cdn)
    original = [dict(c) for c in chs]
    _reclassify_uhd(chs)
    smart_ids = {c["uuid"] for c in chs}
    smart_programs = [p for p in prs if p["uuid"] in smart_ids]
    if epg_enable:
        _guard_epg_coverage(previous, chs, smart_programs)
    minimal = cfg["output_mode"] == "minimal"
    write_outputs(build, cdn, chs, smart_programs, cfg["backup_days"], cfg["days"], base_url, minimal, epg_enable)
    total, unique = write_orig_outputs(build, cdn, original, prs, cfg["backup_days"], base_url)
    log("      原版分组清单：%d 条 / %d 个频道" % (total, unique))
    # ---- 第三套：自定义分组（首次按上游 11 组种子）
    layout = load_layout()
    mine_state = "user"
    if not isinstance(layout, dict) or not layout.get("groups"):
        layout = seed_layout(original)
        mine_state = "seeded"
        try:
            save_layout(layout)
        except Exception as e:      # noqa: BLE001
            log("[自定义分组] 种子写入失败: %s" % e)
    member = {c["uuid"]: (c.get("otypes") or []) for c in original}
    # 供网页编辑器求值「属于上游组」规则时使用，避免前端另存一份归属表
    try:
        with open(os.path.join(build, "orig_member.json"), "w", encoding="utf-8") as f:
            json.dump(member, f, ensure_ascii=False)
    except Exception as e:      # noqa: BLE001
        log("[自定义分组] 归属表写入失败: %s" % e)
    mine_entries, mine_stats = apply_mine_layout(original, layout, member)
    if not mine_entries:
        # 配置把频道全隐藏了：不阻断更新，回退成原版分组并留痕
        layout_used = seed_layout(original)
        mine_entries, mine_stats = apply_mine_layout(original, layout_used, member)
        mine_state = "fallback:empty"
        log("[自定义分组] 配置求值为空，已回退原版分组")
    else:
        layout_used = layout
    mine_total, mine_unique = write_mine_outputs(build, cdn, mine_entries,
                                                 cfg["backup_days"], base_url, epg_enable,
                                                 layout_used)
    log("      自定义分组清单：%d 条 / %d 个频道（%s）" % (mine_total, mine_unique, mine_state))
    detail = dict(detail, privacy="clean")  # 只有后续整代检查通过才会发布这份元数据
    meta = {
        "generation": os.path.basename(build), "epg_enabled": epg_enable,
        "output_mode": cfg["output_mode"], "updated": datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S"),
        "cdn": cdn, "channels": len(chs),
        "with_catchup": sum(1 for ch in chs if ch["back"] > 0),
        "programs": len(smart_programs), "backup_days": cfg["backup_days"],
        "epg_programs": _count_epg(build), "status": "ok", "check": detail,
        "uhd": sum(1 for ch in chs if ch.get("group") == GROUP_UHD),
        "uhd_real": sum(1 for ch in chs if ch.get("is_4k")),
        "uhd_h265": sum(1 for ch in chs if ch.get("group") == GROUP_UHD and "265" in (ch.get("codec") or "")),
        # 孤儿频道（上游未归任何分组，靠名称兜底）
        "mine": {"state": mine_state, "groups": mine_stats["groups"],
                 "entries": mine_stats["entries"], "channels": mine_stats["channels"],
                 "multi": mine_stats["multi"], "hidden": mine_stats["hidden"],
                 "ungrouped": mine_stats["ungrouped"]},
        "orphans": len(ORPHANS),
        "orphan_list": [dict(v) for v in ORPHANS.values()],
    }
    with open(os.path.join(build, "meta.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    # 「4k.json」是给界面与外部播放器直接用的 4K 清单，始终产出
    with open(os.path.join(build, "4k.json"), "w", encoding="utf-8", newline="\n") as f:
        # 4K 组清单：包含真 4K 与「高清臻享」（后者用 real_4k=false 标注真实分辨率）
        k4 = sorted([ch for ch in chs if ch.get("group") == GROUP_UHD], key=gkey)
        json.dump({"updated": meta["updated"], "cdn": cdn,
                   "note": "real_4k=true 为实测 3840x2160；高清臻享中部分为 1080P，见 w/h",
                   "channels": [{"name": ch["name"], "num": ch["num"], "uuid": ch["uuid"],
                                 "real_4k": bool(ch.get("is_4k")),
                                 "unverified": bool(ch.get("uhd_unverified")), "zhx": bool(ch.get("zhx")),
                                 "codec": ch.get("codec") or "",
                                 "w": ch.get("w"), "h": ch.get("h"),
                                 "live": "%s/%s/live1/%s/%s/1.m3u8"
                                         % (cdn, ch["cp"], ch["code"], ch["code"])}
                                for ch in k4]},
                  f, ensure_ascii=False, indent=2)
    validate_generation(build, minimal, epg_enable)
    log("[4/5] 整代文件及隐私检查通过")
    return meta


def do_update(cfg, base_url, reason="manual"):
    with _lock:
        if _updating["running"]:
            return {"ok": False, "msg": "更新正在进行中"}
        _updating["running"] = True
        _updating["started"] = int(time.time())
        _updating["error"] = ""
    log("=== 更新开始（%s）===" % reason)
    try:
        meta = run_once(cfg, base_url)
        return {"ok": True, "meta": meta}
    except Exception as e:      # noqa: BLE001
        log("!! 更新失败: %s" % e)
        _updating["error"] = str(e)
        return {"ok": False, "msg": str(e)}
    finally:
        _updating["running"] = False


_sched = {"stop": None, "thread": None}


def _schedule_loop(cfg, base_url):
    if not cfg.get("schedule_enabled", True):
        _sched["next_run"] = ""
        log("[定时] 自动更新已关闭")
        # 仍保持线程存活，等待重新开启
        stop = _sched.get("stop")
        while stop is not None and not stop.wait(30):
            pass
        return
    at = cfg.get("update_at", "03:30")
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(at))
    hh, mm = (int(m.group(1)), int(m.group(2))) if m else (3, 30)
    while True:
        stop = _sched.get("stop")
        if stop is not None and stop.is_set():
            return
        now = datetime.now(TZ8)
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        gap = (target - now).total_seconds()
        _sched["next_run"] = target.strftime("%Y-%m-%d %H:%M")
        log("[定时] 下次更新 %s（%.1f 小时后）" % (target.strftime("%m-%d %H:%M"), gap / 3600))
        # 用 stop 事件等待，可被配置变更立刻唤醒
        if stop is not None:
            if stop.wait(gap + 20):
                return
        else:
            time.sleep(gap + 20)
        c = load_config()
        do_update(c, base_url, reason="scheduled")


def _kick_scheduler(cfg, base_url):
    """配置变更后重排定时任务：停旧的，起新的"""
    old = _sched.get("stop")
    if old is not None:
        old.set()
    _sched["next_run"] = ""
    stop = threading.Event()
    _sched["stop"] = stop
    t = threading.Thread(target=_schedule_loop, args=(cfg, base_url), daemon=True)
    _sched["thread"] = t
    t.start()


# ---------------------------------------------------------------- 直播代理
# 运营商 CDN 的 m3u8 / ts 不带 Access-Control-Allow-Origin，
# 桌面窗口(iframe)里浏览器直连必然失败，因此由本地服务端代取并改写播放列表。

_stream_cache = {}
_cache_lock = threading.Lock()


def _is_media_host(host):
    """只放行运营商 CDN / 内网主机，防止被当成开放代理。"""
    if not host:
        return False
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        ip = host
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        return False
    o = ip.split(".")
    if o[0] in ("223", "221", "112", "120", "121"):
        return True
    if ip.startswith(("127.", "10.", "192.168.")):
        return True
    if ip.startswith("172.") and 16 <= int(o[1]) <= 31:
        return True
    return False


def _fetch(url, timeout=12, range_header=""):
    """打开上游流并转发单段 Range；调用方负责流式读取。

    返回 (stream, final_url, headers)；调用方负责 close。
    """
    h = {"User-Agent": STREAM_UA, "Accept": "*/*", "Connection": "close"}
    if range_header:
        h["Range"] = range_header
    req = urllib.request.Request(url, headers=h)
    r = urllib.request.urlopen(req, timeout=timeout)
    return r, r.geturl(), r.headers


def _abs(base, ref):
    return urllib.parse.urljoin(base, ref)


def _rewrite_m3u8(body, final_url, proxy_base):
    """把 m3u8 中的切片/子清单地址改写成走本地 /stream 代理"""
    out = []
    for line in body.splitlines():
        t = line.strip()
        if not t:
            out.append(line)
        elif t.startswith("#"):
            # 处理 #EXT-X-KEY / #EXT-X-MAP 中的 URI="..."
            if 'URI="' in t:
                def _fix(m):
                    u = _abs(final_url, m.group(1))
                    return 'URI="%s"' % (proxy_base + urllib.parse.quote(u, safe=""))
                t = re.sub(r'URI="([^"]+)"', _fix, t)
            out.append(t)
        else:
            u = _abs(final_url, t)
            out.append(proxy_base + urllib.parse.quote(u, safe=""))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- HTTP 服务

READ_ONLY_FILES = {
    "iptv.m3u8", "epg.xml", "tvbox.json", "iptv_fongmi.m3u8", "EPG.xml",
    "fongmi.m3u", "fongmi.json",
    "直播源.txt", "直播源.m3u", "直播源_kodi.m3u", "直播源_tvbox_fongmi.m3u", "直播源.json",
    "组播源.m3u", "频道清单.csv", "节目源对照.csv", "回看清单.csv",
    "频道对照_机顶盒顺序.csv", "回看节目单.csv", "meta.json",
    "4k.json",          # 4K 频道清单（含实测编码），对外可直接取用
    "channels.json",
    # 方案二：上游原版清单（11 组原样）
    "orig.m3u8", "orig_epg.xml", "orig.json",
    # 方案三：用户自定义分组
    "mine.m3u8", "mine_kodi.m3u", "mine_fongmi.m3u", "mine.json",
    # ASCII 别名（真实文件另见 ALIASES，部分模式会直接落盘）
    "kodi.m3u", "live.m3u", "live.txt", "diyp.m3u", "fongmi.m3u", "multicast.m3u",
}

# 纯 ASCII 别名 -> 真实中文文件名。
# 电视端播放器（Kodi/DIYP/部分盒子）对 URL 里的裸中文经常处理不好，
# 因此对外一律建议用 ASCII 地址，内部仍存原始中文文件名方便用户从共享目录翻找。
ALIASES = {
    "kodi.m3u": "直播源_kodi.m3u",
    "diyp.m3u": "直播源.m3u",
    "live.m3u": "直播源.m3u",
    "fongmi.m3u": "直播源_tvbox_fongmi.m3u",
    "live.txt": "直播源.txt",
    "multicast.m3u": "组播源.m3u",
}
API_MIME = "application/json; charset=utf-8"
STREAM_UA = "okhttp/3.12.0"


# ---------------------------------------------------------------- 实时节目状态
# channels.json 由定时任务每天生成一次，里面的 now/next 是「生成那一刻」的快照。
# 界面要的是「此刻正在播什么」，所以每次请求都按当前时间重算这两档。
# 缓存 programs.json 的解析结果（几 MB，别每次请求都 parse），
# 以文件 mtime 作为失效条件 —— 更新任务替换文件后会自动重新载入。

_prog_cache = {"mtime": None, "data": None}
_prog_lock = threading.Lock()


def _programs_index(output_dir):
    p = os.path.join(output_dir, "programs.json")
    try:
        mt = (p, os.stat(p).st_mtime_ns)
    except OSError:
        return {}
    with _prog_lock:
        if _prog_cache["mtime"] == mt and _prog_cache["data"] is not None:
            return _prog_cache["data"]
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    with _prog_lock:
        _prog_cache["mtime"], _prog_cache["data"] = mt, data
    return data


def _refresh_now_next(chs, output_dir, nowts=None):
    """把每路的 now / next 换成按当前时间实算的结果（原地修改）。"""
    nowts = int(time.time()) if nowts is None else nowts
    idx = _programs_index(output_dir)
    for c in chs:
        ps = idx.get(c.get("uuid")) or []
        cur = nxt = None
        for p in ps:
            if p["start"] <= nowts < p["end"]:
                cur = p
            elif p["start"] > nowts:
                nxt = p
                break
        c["now"] = {"t": cur["title"], "s": cur["start"], "e": cur["end"]} if cur else None
        c["next"] = {"t": nxt["title"], "s": nxt["start"], "e": nxt["end"]} if nxt else None


class Handler(http.server.SimpleHTTPRequestHandler):
    cfg = None
    base_url = ""

    def __init__(self, *a, **kw):
        self.output_dir = _output_dir()  # 请求内不跨代读取
        super().__init__(*a, directory=self.output_dir, **kw)

    def do_HEAD(self):
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self.end_headers()

    # ---- 输出工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", API_MIME)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype, cache="no-store", hint_name=""):
        p = os.path.join(self.output_dir, path)
        if not os.path.isfile(p):
            msg = "文件尚未生成，请先执行一次更新"
            if hint_name and hint_name != path:
                msg = ("该文件在当前输出模式下不生成（当前为精简模式）。"
                       "如需 Kodi/DIYP 专用格式，请在「应用设置」中把输出模式改为「完整」。")
            self._json({"error": msg, "file": hint_name or path}, 404)
            return
        data = open(p, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _logo(self, fn):
        if not re.match(r"^[0-9a-f]{12}\.png$", fn):
            self.send_error(404)
            return
        p = os.path.join(self.output_dir, "logo", fn)
        if not os.path.isfile(p):
            self.send_error(404)
            return
        data = open(p, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _epg_day(self, uid, day):
        """FongMi 按天节目单。查不到该频道/该天时返回空表而不是 404，
        否则 FongMi 面板会一直转圈等不到响应。"""
        if not _valid_epg_uid(uid):
            self._json({"error": "bad uuid"}, 400)
            return
        p = os.path.join(self.output_dir, "epg", uid, day + ".json")
        if os.path.isfile(p):
            data = open(p, "rb").read()
        else:
            data = json.dumps({"date": day, "epg_data": []},
                              ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", API_MIME)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _self_base(self):
        """用当前请求的 Host 推导外部访问地址；优先于启动时探测的 base_url。"""
        h = (self.headers.get("Host") or "").strip()
        if not h:
            return self.base_url
        if h.startswith("localhost") or h.startswith("127.0.0.1"):
            return self.base_url or ("http://" + h)
        return "http://" + h

    def _safe_meta(self):
        try:
            with open(os.path.join(self.output_dir, "meta.json"), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _stream(self, qs):
        """直播/回看流代理。只放行江苏移动 CDN 主机，避免变成开放代理。

        4K 频道黑屏（有声音无画面）的三条根因都在这里：
          ① 以前整段读完再回写：4K 单切片 20~45MB，前端分片请求全部超时，
             播放器拿不到足够数据 → 只出音轨、视频轨永远缓冲 → 纯黑。
          ② 不转发 Range：播放器/浏览器 seek 或分段拉取时上游返回全量，
             时序错乱，同样表现为「有声音、画面不动」。
          ③ 上游 Content-Type 被改写：H.265 流有的档返回 video/mp2t、
             有的返回 application/octet-stream，前端按错误类型解析。
        这里改成流式分块转发 + Range 透传 + Content-Type 原样返回。
        """
        u = (qs.get("u", [""])[0] or "").strip()
        if not u:
            self.send_error(400)
            return
        if not re.match(r"^https?://[0-9a-zA-Z.\-]+(:\d+)?/", u):
            self.send_error(400)
            return
        host = urllib.parse.urlparse(u).hostname or ""
        if not _is_media_host(host):
            self.send_error(403)
            return

        # 保留单段 Range 的原始语义（含 0- 和后缀范围）。
        rng = (self.headers.get("Range") or "").strip()
        if rng and not re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", rng):
            self.send_error(400, "Unsupported Range")
            return
        try:
            r, final, hdrs = _fetch(u, range_header=rng)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                self.send_response(416)
                if e.headers.get("Content-Range"):
                    self.send_header("Content-Range", e.headers["Content-Range"])
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error(502)
            e.close()
            return
        except Exception as e:      # noqa: BLE001
            log("[代理] 取流失败 %s -> %s" % (u[:90], e))
            self.send_error(502)
            return

        started = False
        try:
            ctype = (hdrs.get("Content-Type") or "").split(";")[0].strip()
            # 播放列表：必须整份读（几 KB），并改写内部地址
            head = r.read(200)
            if head.startswith(b"#EXTM3U") or "mpegurl" in ctype.lower():
                rest = r.read(1024 * 1024)
                if len(head) + len(rest) > 1024 * 1024:
                    self.send_error(502, "Upstream playlist too large")
                    return
                proxy_base = "/stream?u="
                txt = _rewrite_m3u8((head + rest).decode("utf-8", "replace"), final, proxy_base)
                body = txt.encode("utf-8")
                started = True
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return

            # 媒体分片：流式转发，边收边发，不等整段
            total = hdrs.get("Content-Length")
            started = True
            self.send_response(r.status)
            self.send_header("Content-Type", ctype or "video/mp2t")
            if total:
                self.send_header("Content-Length", total)
            if hdrs.get("Content-Range"):
                self.send_header("Content-Range", hdrs.get("Content-Range"))
            if hdrs.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", hdrs["Accept-Ranges"])
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(head)
            sent = len(head)
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
            # 上游长度比声明少时（直播切片正在写入），以实际发送为准
            if total and str(sent) != total:
                self.close_connection = True
                log("[代理] 上游媒体长度不符（收到 %d 字节）" % sent)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # 播放器切台/seek 时会主动断开
        except (OSError, http.client.HTTPException) as e:
            self.close_connection = True
            log("[代理] 上游媒体读取中断（%s）" % type(e).__name__)
            if not started:
                self.send_error(502)
        finally:
            try:
                r.close()
            except Exception:
                pass

    # ---- 路由
    def do_GET(self):
        # 统一异常兜底：任何未捕获异常都返回 JSON 错误，避免连接被直接断开
        try:
            self._do_GET()
        except Exception as e:      # noqa: BLE001
            log("[错误] GET %s -> %r" % (self.path[:120], e))
            try:
                self._json({"ok": False, "error": "服务端内部错误", "detail": str(e)[:200]}, 500)
            except Exception:
                pass

    def _do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        qs = urllib.parse.parse_qs(u.query)

        if path.startswith("/logo/"):
            self._logo(path[6:])
            return

        if path == "/stream":
            self._stream(qs)
            return

        # ---- API
        if path == "/api/status":
            cfg = load_config()
            meta = self._safe_meta()
            # 分组构成：直接数 channels.json，保证与「节目源」页完全一致
            gcount = {}
            allc = []
            try:
                with open(os.path.join(self.output_dir, "channels.json"), encoding="utf-8") as f:
                    allc = json.load(f)
                for c in allc:
                    gcount[c.get("group", "其他")] = gcount.get(c.get("group", "其他"), 0) + 1
            except Exception:
                pass
            try:
                logos = sum(1 for f in os.listdir(os.path.join(self.output_dir, "logo"))
                            if re.match(r"^[0-9a-f]{12}\.png$", f))
            except Exception:
                logos = 0
            self._json({"ok": True, "version": _runtime_version(), "config": cfg, "meta": meta,
                        "updating": _updating, "base_url": self._self_base(),
                        "facets": {
                            "top": dict(sorted(gcount.items(), key=lambda kv: -kv[1])),
                            "g_uhd": gcount.get(GROUP_UHD, 0),
                            "g_uhd_real": sum(1 for c in allc if c.get("is_4k")),
                            "g_cctv": gcount.get("央视", 0),
                            "g_tv": gcount.get("卫视", 0),
                            "g_js": gcount.get("江苏", 0),
                            "g_local": sum(v for k, v in gcount.items() if k.startswith("地方")),
                            "g_movie": gcount.get("影视剧场", 0),
                            "g_kids": gcount.get("少儿动画", 0),
                            "g_sport": gcount.get("体育竞技", 0),
                            "g_life": gcount.get("生活纪录", 0),
                            "g_radio": gcount.get("音乐广播", 0),
                            "g_shop": gcount.get("购物", 0),
                            "g_other": gcount.get("其他", 0),
                            "logos": logos,
                            "cdn_candidates": len(HOSTS),
                            # 分组顺序即界面顺序：4K 置顶 → 央视 → 卫视 → 江苏 → 地方 → 其它
                            "order": [g for g in GROUP_ORDER],
                        },
                        "schedule": {"enabled": bool(cfg.get("schedule_enabled", True)),
                                     "at": cfg.get("update_at", "03:30"),
                                     "next_run": _sched.get("next_run", "")},
                        "server_time": datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")})
            return

        if path == "/api/channels":
            g = qs.get("group", [""])[0]
            kw = qs.get("q", [""])[0].strip().lower()
            try:
                with open(os.path.join(self.output_dir, "channels.json"), encoding="utf-8") as f:
                    chs = json.load(f)
            except Exception:
                self._json({"error": "频道表尚未生成，请先执行一次更新"}, 404)
                return
            # ---- 右侧列表的「当前节目」必须实时算，不能读 channels.json 里的存量值。
            # channels.json 是每天 03:30 生成时写死的 now/next，之后一整天都不会变，
            # 于是界面上的节目名永远停在凌晨那一档 —— 这就是「EPG 不实时更新」的根因。
            _refresh_now_next(chs, self.output_dir)
            # 三套分组方案：首页切换时在同一份频道数据上换分组标签与组顺序，
            # 频道地址、节目单不变，用户可以先检查再决定用哪一套。
            view = qs.get("view", ["smart"])[0]
            order = list(GROUP_ORDER)
            if view in ("orig", "mine"):
                try:
                    with open(os.path.join(self.output_dir, "orig_member.json"), encoding="utf-8") as f:
                        member = json.load(f)
                except Exception:
                    member = {}
                if view == "orig":
                    by_uuid = {c["uuid"]: c for c in chs}
                    seq, seen = [], set()
                    for _aid, gname in ORIG_GROUPS:
                        rows = [c for c in chs if gname in (member.get(c["uuid"]) or [])]
                        rows.sort(key=lambda c: int(re.sub(r"\D", "", str(c.get("num", ""))) or 0))
                        for c in rows:
                            if c["uuid"] in seen:
                                continue      # 列表里一台只显示一次，清单里仍按上游多组输出
                            seen.add(c["uuid"])
                            c = dict(c, group=gname)
                            by_uuid[c["uuid"]] = c
                            seq.append(c)
                    missing = [c for c in chs if c["uuid"] not in seen]
                    for c in missing:
                        seq.append(dict(c, group="其他"))
                    chs = seq
                    order = [g for _a, g in ORIG_GROUPS] + (["其他"] if missing else [])
                else:
                    layout = load_layout()
                    if not isinstance(layout, dict) or not layout.get("groups"):
                        layout = seed_layout(chs)
                    entries, _st = apply_mine_layout(chs, layout, member)
                    by_uuid = {c["uuid"]: c for c in chs}
                    seq, seen = [], set()
                    for ch, title in entries:
                        if ch["uuid"] in seen:
                            continue
                        seen.add(ch["uuid"])
                        seq.append(dict(by_uuid[ch["uuid"]], group=title))
                    for c in chs:
                        if c["uuid"] not in seen and not any(
                                _mine_match(c, r, member) for r in (layout.get("hide") or [])):
                            seq.append(dict(c, group="其他"))
                    chs = seq
                    titles = [g["title"] for g in layout["groups"]]
                    order = [t for t in titles if any(c["group"] == t for c in chs)]
                    if any(c["group"] == "其他" for c in chs):
                        order.append("其他")
            groups = {}
            for c in chs:
                groups[c["group"]] = groups.get(c["group"], 0) + 1
            if g and g != "全部":
                chs = [c for c in chs if c["group"] == g]
            if kw:
                chs = [c for c in chs if kw in c["name"].lower() or kw in c["uuid"].lower()
                       or kw in str(c.get("num", ""))]
            self._json({"ok": True, "total": len(chs), "groups": groups, "view": view,
                        "generation": self._safe_meta().get("generation"),
                        "order": order, "channels": chs})
            return

        if path == "/api/layout":
            original = []
            try:
                with open(os.path.join(self.output_dir, "channels.json"), encoding="utf-8") as f:
                    original = json.load(f)
            except Exception:
                self._json({"ok": False, "msg": "频道表尚未生成，请先执行一次更新"}, 404)
                return
            layout = load_layout()
            seeded = False
            if not isinstance(layout, dict) or not layout.get("groups"):
                layout = seed_layout(original)
                seeded = True
            # 给界面用：每个分组当前命中几台、按什么顺序输出
            member = {}
            try:
                with open(os.path.join(self.output_dir, "orig_member.json"), encoding="utf-8") as f:
                    member = json.load(f)
            except Exception:
                member = {}
            entries, stats = apply_mine_layout(original, layout, member)
            hits = {}
            for ch, title in entries:
                hits[title] = hits.get(title, 0) + 1
            titles = [g["title"] for g in layout["groups"]]
            self._json({"ok": True, "layout": layout, "seeded": seeded,
                        "stats": stats,
                        "groups": [{"title": t, "hits": hits.get(t, 0),
                                    "in_output": hits.get(t, 0) > 0} for t in titles]
                                  + ([{"title": "其他", "hits": stats["ungrouped"],
                                       "in_output": stats["ungrouped"] > 0}] if stats["ungrouped"] else []),
                        "orig_groups": [g for _a, g in ORIG_GROUPS],
                        "limit": {"groups": MINE_GROUP_LIMIT, "title": MINE_TITLE_LIMIT,
                                  "rules": MINE_RULE_LIMIT},
                        "outputs": {"m3u": "/mine.m3u8", "kodi": "/mine_kodi.m3u",
                                    "fongmi": "/mine_fongmi.m3u", "json": "/mine.json"}})
            return

        if path == "/api/epg":
            uid = qs.get("uuid", [""])[0]
            if not uid:
                self._json({"error": "缺少 uuid"}, 400)
                return
            if not os.path.isfile(os.path.join(self.output_dir, "programs.json")):
                self._json({"error": "节目单尚未生成"}, 404)
                return
            pp = _programs_index(self.output_dir)
            ps = pp.get(uid, [])
            day = qs.get("date", [""])[0]
            if day:
                lo = datetime.strptime(day, "%Y%m%d").replace(tzinfo=TZ8)
                hi = lo + timedelta(days=1)
                ps = [p for p in ps if p["end"] > int(lo.timestamp()) and p["start"] < int(hi.timestamp())]
            self._json({"ok": True, "uuid": uid, "count": len(ps), "programs": ps,
                        "generation": self._safe_meta().get("generation")})
            return

        if path == "/api/schedule":
            cfg = load_config()
            self._json({"ok": True,
                        "enabled": bool(cfg.get("schedule_enabled", True)),
                        "at": cfg.get("update_at", "03:30"),
                        "next_run": _sched.get("next_run", ""),
                        "meta": self._safe_meta()})
            return

        # 回看地址生成：把节目起止时间套进 catchup 模板
        if path == "/api/catchup":
            uid = qs.get("uuid", [""])[0]
            st = qs.get("start", [""])[0]
            en = qs.get("end", [""])[0]
            if not uid or not st or not en:
                self._json({"ok": False, "msg": "缺少 uuid/start/end"}, 400)
                return
            ch = None
            try:
                with open(os.path.join(self.output_dir, "channels.json"), encoding="utf-8") as f:
                    idx = json.load(f)
                for c in idx:
                    if c.get("uuid") == uid:
                        ch = c
                        break
            except Exception:
                pass
            if not ch:
                self._json({"ok": False, "msg": "频道不存在，请先更新"}, 404)
                return
            if not ch.get("catchup"):
                self._json({"ok": False, "msg": "该频道不支持回看"}, 400)
                return
            try:
                s = int(st)
                e = int(en)
            except Exception:
                self._json({"ok": False, "msg": "时间参数错误"}, 400)
                return
            if e <= s:
                self._json({"ok": False, "msg": "结束时间必须晚于开始时间"}, 400)
                return
            # 以已发布清单的设置为准；实际上游存档跨度需实测。
            if e > time.time() + 60:
                self._json({"ok": False, "msg": "该节目尚未开始，无法回看"}, 400)
                return
            backup_days = self._safe_meta().get("backup_days", DEFAULTS["backup_days"])
            if time.time() - s > backup_days * 86400:
                self._json({"ok": False, "msg": "该节目已超过配置的回看天数"}, 400)
                return
            fa = datetime.fromtimestamp(s, TZ8).strftime("%Y%m%d%H%M%S")
            fb = datetime.fromtimestamp(e, TZ8).strftime("%Y%m%d%H%M%S")
            # 模板里的占位符写法随清单版本变过（裸 {start} / ${start} / ${(b)fmt}），
            # 老数据也可能残留，所以不按字面 replace，而是找出所有 {...} 逐个替换：
            # 起时间类（start/b）→ fa，止时间类（end/e）→ fb，其余原样保留。
            def _sub(m):
                tok = m.group(0).lower()
                if "start" in tok or "(b" in tok or "{b" in tok:
                    return fa
                if "end" in tok or "(e" in tok or "{e" in tok or "utcend" in tok:
                    return fb
                return m.group(0)

            url = re.sub(r"\$?\{[^}]+\}", _sub, ch["catchup"])
            self._json({"ok": True, "url": url,
                        "proxy": "/stream?u=" + urllib.parse.quote(url, safe=""),
                        "name": ch.get("name", ""), "from": fa, "to": fb})
            return

        if path == "/api/spec":
            # 返回某一路直播流的真实规格（分辨率/编码/声道/码率）。
            # 每次都现场探测并缓存 30 分钟：直播流参数可能随时变，
            # 但 30 分钟内同一频道反复查询没必要重测。
            u = (qs.get("u") or [""])[0]
            if not u:
                self._json({"ok": False, "msg": "缺少 u 参数"}, 400)
                return
            host = urllib.parse.urlparse(u).hostname or ""
            if not _is_media_host(host):
                self._json({"ok": False, "msg": "只允许探测运营商媒体源"}, 403)
                return
            now = time.time()
            with _spec_lock:
                hit = _SPEC_CACHE.get(u)
                if hit and now - hit[0] < _SPEC_TTL:
                    self._json({"ok": True, "spec": hit[1], "cached": True})
                    return
            if _mp is None:
                self._json({"ok": False, "msg": "探测模块未加载"}, 501)
                return
            try:
                info = _mp.probe_url(u, timeout=10, max_bytes=2500000)
            except Exception as e:
                self._json({"ok": False, "msg": "探测失败: %s" % e}, 502)
                return
            with _spec_lock:
                _SPEC_CACHE[u] = (now, info)
                if len(_SPEC_CACHE) > 300:
                    for k in list(_SPEC_CACHE)[:100]:
                        _SPEC_CACHE.pop(k, None)
            # 前端要不要提示「此流本机播不了」由这里给结论，避免界面自己猜编码。
            # 依据：video.codec 是实测出来的，不是按名字猜的。
            v = info.get("video") or {}
            codec = (v.get("codec") or "").upper()
            info["playable_hint"] = "h265" if "265" in codec else ("h264" if "264" in codec else "")
            self._json({"ok": True, "spec": info})
            return

        if path == "/api/log":
            n = int(qs.get("lines", ["120"])[0] or 120)
            self._json({"ok": True, "log": _tail_log(n)})
            return

        if path == "/api/update":
            cfg = load_config()
            res = do_update(cfg, self._self_base(), reason="api")
            self._json(res, 200 if res.get("ok") else 500)
            return

        if path == "/api/discover":
            found = discover()
            self._json({"ok": True, "found": found})
            return

        # ---- FongMi 按天节目单：/epg/<uuid>/<YYYY-MM-DD>.json
        m_epg = re.match(r"^/epg/([^/]+)/([0-9]{4}-[0-9]{2}-[0-9]{2})\.json$", path)
        if m_epg:
            self._epg_day(m_epg.group(1), m_epg.group(2))
            return

        # ---- 静态数据文件
        name = path.lstrip("/")
        if name in READ_ONLY_FILES:
            mimes = {".m3u8": "application/vnd.apple.mpegurl", ".m3u": "audio/x-mpegurl",
                     ".xml": "application/xml; charset=utf-8", ".json": API_MIME,
                     ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
            ext = os.path.splitext(name)[1].lower()
            # ASCII 别名：文件没落盘时回落到对应的中文源文件，保证地址永远可用
            real = name
            if name in ALIASES and not os.path.isfile(os.path.join(self.output_dir, name)):
                real = ALIASES[name]
            self._file(real, mimes.get(ext, "application/octet-stream"),
                       hint_name=name)
            return

        if name in ("", "index.html", "ui"):
            self._ui()
            return

        # www 下的静态资源（hls.min.js 等），扩展名白名单在 _asset 内校验
        if os.path.splitext(name)[1].lower() in (".js", ".css", ".png", ".svg", ".woff2", ".ico"):
            self._asset(name)
            return

        self._json({"error": "Not Found"}, 404)

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:      # noqa: BLE001
            log("[错误] POST %s -> %r" % (self.path[:120], e))
            try:
                self._json({"ok": False, "error": "服务端内部错误", "detail": str(e)[:200]}, 500)
            except Exception:
                pass

    def _do_POST(self):
        u = urllib.parse.urlparse(self.path)

        if u.path == "/api/schedule":
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                self._json({"ok": False, "msg": "请求格式错误"}, 400)
                return
            cfg = load_config()
            if "enabled" in body:
                cfg["schedule_enabled"] = bool(body["enabled"])
            if body.get("at"):
                at = str(body["at"]).strip()
                if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", at):
                    self._json({"ok": False, "msg": "时间格式应为 HH:MM"}, 400)
                    return
                h, m = at.split(":")
                cfg["update_at"] = "%02d:%s" % (int(h), m)
            save_config(cfg)
            _kick_scheduler(cfg, self._self_base())
            self._json({"ok": True, "enabled": cfg.get("schedule_enabled", True),
                        "at": cfg.get("update_at", "03:30"),
                        "next_run": _sched.get("next_run", "")})
            return

        if u.path == "/api/update":
            cfg = load_config()
            res = do_update(cfg, self._self_base(), reason="api")
            self._json(res, 200 if res.get("ok") else 500)
            return
        if u.path == "/api/layout":
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 262144:
                self._json({"ok": False, "msg": "请求体为空或过大"}, 400)
                return
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception as e:      # noqa: BLE001
                self._json({"ok": False, "msg": "JSON 解析失败: %s" % e}, 400)
                return
            layout, err = validate_layout(body)
            if err:
                self._json({"ok": False, "msg": err}, 400)
                return
            try:
                old = load_layout()
                if isinstance(old, dict):
                    with open(_layout_path() + ".prev", "w", encoding="utf-8") as f:
                        json.dump(old, f, ensure_ascii=False, indent=2)
                save_layout(layout)
            except Exception as e:      # noqa: BLE001
                self._json({"ok": False, "msg": "写入布局失败: %s" % e}, 500)
                return
            log("[自定义分组] 布局已保存：%d 组 / %d 条规则"
                % (len(layout["groups"]), sum(len(g["rules"]) for g in layout["groups"])))
            self._json({"ok": True, "layout": layout})
            return

        if u.path == "/api/config":
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                self._json({"ok": False, "msg": "请求体为空或过大"}, 400)
                return
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception as e:      # noqa: BLE001
                self._json({"ok": False, "msg": "JSON 解析失败: %s" % e}, 400)
                return
            cur = load_config()
            restart = False
            for k in ("update_at", "district", "days", "history_days",
                      "backup_days", "localize_logo", "epg_enable", "output_mode", "port"):
                if k not in body:
                    continue
                v = body[k]
                if k == "update_at":
                    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", str(v)):
                        self._json({"ok": False, "msg": "时间格式应为 HH:MM"}, 400)
                        return
                    v = str(v)
                elif k in ("days", "history_days", "backup_days", "port"):
                    try:
                        v = int(v)
                    except Exception:
                        self._json({"ok": False, "msg": "%s 必须是数字" % k}, 400)
                        return
                    if k == "port" and not (1024 <= v <= 65535):
                        self._json({"ok": False, "msg": "端口需在 1024–65535"}, 400)
                        return
                    if k != "port" and not (0 <= v <= 30):
                        self._json({"ok": False, "msg": "%s 需在 0–30" % k}, 400)
                        return
                    if k == "port" and v != cur.get("port"):
                        restart = True
                elif k in ("localize_logo", "epg_enable"):
                    if not isinstance(v, bool):
                        self._json({"ok": False, "msg": "%s 必须是布尔值" % k}, 400)
                        return
                elif k == "district":
                    v = str(v)
                elif k == "output_mode":
                    if v not in ("minimal", "full"):
                        self._json({"ok": False, "msg": "输出模式只能是 minimal/full"}, 400)
                        return
                cur[k] = v
            try:
                os.makedirs(PKGETC, exist_ok=True)
                tmp = CONF_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CONF_FILE)
            except Exception as e:      # noqa: BLE001
                self._json({"ok": False, "msg": "写入配置失败: %s" % e}, 500)
                return
            log("[配置] 已更新: %s" % json.dumps(
                {k: cur.get(k) for k in ("update_at", "district", "days",
                                         "history_days", "backup_days",
                                         "localize_logo", "epg_enable", "output_mode", "port")},
                ensure_ascii=False))
            # 重新排程：终止旧 scheduler，起新的（端口变更时由用户重启，避免中途断连）
            if not restart:
                _kick_scheduler(cur, self._self_base())
            self._json({"ok": True, "restart": restart, "config": cur})
            return
        self._json({"error": "Not Found"}, 404)

    def _ui(self):
        p = os.path.join(WWW, "index.html")
        if not os.path.isfile(p):
            self.send_error(404, "UI missing")
            return
        data = open(p, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _asset(self, name):
        """www 目录下的静态资源（js/css/字体等）。
        只允许白名单扩展名，且必须落在 WWW 目录内，防目录穿越。"""
        ext = os.path.splitext(name)[1].lower()
        mimes = {".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".png": "image/png", ".svg": "image/svg+xml",
                 ".woff2": "font/woff2", ".ico": "image/x-icon"}
        if ext not in mimes:
            self._json({"error": "Not Found"}, 404)
            return
        root = os.path.realpath(WWW)
        p = os.path.realpath(os.path.join(root, name))
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            self._json({"error": "Not Found"}, 404)
            return
        data = open(p, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", mimes[ext])
        self.send_header("Content-Length", str(len(data)))
        # 应用入口资源随包更新，不沿用浏览器里的旧 UI；第三方固定资源仍可缓存。
        self.send_header("Cache-Control", "no-cache" if name in ("workspace.css", "workspace.js")
                         else "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, *a):
        pass


def serve(cfg, base_url, initial_update=False):
    port = cfg["port"]
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    Handler.cfg = cfg
    Handler.base_url = base_url
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler) as srv:
        log("[服务] 已启动 :%d  BASE_URL=%s" % (port, base_url))
        if initial_update:
            threading.Thread(target=do_update, args=(cfg, base_url, "startup"), daemon=True).start()
        srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--base-url", default="")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--no-update", action="store_true", help="启动时不立即更新")
    args = ap.parse_args()

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port
    base = (args.base_url or os.environ.get("BASE_URL") or "").strip()
    if not base:
        ip = lan_ip()
        base = "http://%s" % ip if cfg["port"] == 80 else "http://%s:%d" % (ip, cfg["port"])

    os.makedirs(DATA, exist_ok=True)

    if args.discover:
        discover()
        return 0
    if args.once:
        meta = run_once(cfg, base)
        log("完成 %s" % json.dumps(meta, ensure_ascii=False))
        return 0

    initial_update = not args.no_update and not os.path.exists(os.path.join(_output_dir(), "channels.json"))
    if not initial_update:
        log("[启动] 跳过首次更新，交由定时任务")
    _kick_scheduler(cfg, base)
    serve(cfg, base, initial_update=initial_update)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止")