# -*- coding: utf-8 -*-
"""第一轮验收：对明确指定的测试实例做只读 API / 产物检查；不请求媒体地址。"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import re
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
import xml.etree.ElementTree as ET

TZ8 = timezone(timedelta(hours=8))
AP = ("${(b)yyyyMMddHHmmss}", "${(e)yyyyMMddHHmmss}")
KODI = ("${start:YmdHMS}", "${end:YmdHMS}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def absolute_http(url):
    parts = urlsplit(url or "")
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def serves(url, path):
    return absolute_http(url) and urlsplit(url).path == path


def program_time(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S %z")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="测试实例 HTTP(S) origin，不含路径")
    parser.add_argument("--version", required=True, help="本次待验 FPK 的 manifest 版本")
    parser.add_argument("--mode", choices=("full", "minimal"), required=True)
    parser.add_argument("--epg", choices=("on", "off"), required=True)
    parser.add_argument("--generation", help="核对既有产物代号，例如启停后的保留验证")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    target = urlsplit(base)
    if not absolute_http(base) or target.path or target.query or target.fragment or target.username is not None:
        parser.error("--base-url 必须是无路径、参数、凭据的 HTTP(S) origin")
    epg_on = args.epg == "on"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    passed, failed = [], []

    def get(path, raw=False):
        with opener.open(base + path, timeout=40) as response:
            if response.status != 200:
                raise ValueError(f"{path}: HTTP {response.status}")
            body = response.read()
        return body if raw else json.loads(body)

    def ck(name, cond, detail=""):
        (passed if cond else failed).append(name)
        print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail != "" else ""))
        return cond

    def playlist(path, expected, tokens, backup_days):
        lines = get(path, raw=True).decode("utf-8-sig").splitlines()
        header, *body = lines
        has_tvg = bool(re.search(r'\b(?:url-tvg|x-tvg-url)\s*=', header, re.I))
        tvg_urls = re.findall(r'\b(?:url-tvg|x-tvg-url)="([^"]*)"', header, re.I)
        required_tvg = epg_on and args.mode == "minimal" and path != "/orig.m3u8"
        ck(f"{path} M3U 头与 EPG 开关", header.split(maxsplit=1)[0] == "#EXTM3U"
           and (epg_on or not has_tvg) and (not required_tvg or has_tvg)
           and (not has_tvg or (bool(tvg_urls) and all(serves(u, "/orig_epg.xml" if path == "/orig.m3u8" else "/epg.xml") for u in tvg_urls))))
        actual = []
        attrs_ok = True
        pending = None
        for line in body:
            if line.startswith("#EXTINF:"):
                if pending is not None:
                    raise ValueError(f"{path}: 有频道缺直播地址")
                # 只取开头的带引号属性，不让逗号后的频道名干扰解析。
                prefix = re.match(r'#EXTINF:[^\s,]+((?:\s+[\w-]+="[^"]*")*),', line)
                if not prefix:
                    raise ValueError(f"{path}: EXTINF 格式错误")
                pending = dict(re.findall(r'([\w-]+)="([^"]*)"', prefix[1]))
            elif line.strip() and not line.startswith("#"):
                if pending is None:
                    raise ValueError(f"{path}: 直播地址缺 EXTINF")
                uid, group = pending.get("tvg-id"), pending.get("group-title")
                actual.append((group, uid, line.strip()))
                attrs = {k for k in pending if k.startswith("catchup")}
                source = urlsplit(pending.get("catchup-source", ""))
                has_catchup = expected.get((group, uid, line.strip()), False)
                attrs_ok = attrs_ok and (not pending.get("tvg-logo") or absolute_http(pending["tvg-logo"]))
                if has_catchup:
                    attrs_ok = attrs_ok and (pending.get("catchup") == "default"
                        and pending.get("catchup-days") == str(backup_days)
                        and absolute_http(pending.get("catchup-source")) and "/lookback/" in source.path
                        and parse_qs(source.query).get("playseek") == ["-".join(tokens)])
                else:
                    attrs_ok = attrs_ok and not attrs
                pending = None
        ck(f"{path} 分组 / UUID / 直播地址一致", bool(actual) and pending is None
           and Counter(actual) == Counter(expected.keys()), len(actual))
        ck(f"{path} 回看模板与台标地址正确", bool(actual) and attrs_ok)

    def xmltv(path, expected_ids):
        root = ET.fromstring(get(path, raw=True))
        ids = [ch.get("id") for ch in root.findall("channel")]
        programs = root.findall("programme")
        id_set = set(ids)
        valid_ids = expected_ids == id_set if epg_on else (not ids or expected_ids == id_set)
        ck(f"{path} XMLTV 结构 / 频道 ID / 台标", root.tag == "tv" and valid_ids
           and len(ids) == len(id_set) and all(absolute_http(i.get("src")) for i in root.findall("channel/icon")))
        ck(f"{path} 节目单与 EPG 开关一致", bool(programs) == epg_on and all(
            p.get("channel") in id_set and p.findtext("title") and program_time(p.attrib["start"]) < program_time(p.attrib["stop"])
            for p in programs), len(programs))
        return programs

    def status():
        doc = get("/api/status")
        m = doc["meta"]
        ck("状态成功且未在更新", doc.get("ok") is True and m.get("status") == "ok"
           and not doc.get("updating", {}).get("running") and not doc.get("updating", {}).get("error"))
        ck("运行版本与待验包一致", doc.get("version") == args.version, doc.get("version"))
        gen = m.get("generation")
        ck("有效产物代号", isinstance(gen, (str, int)) and not isinstance(gen, bool)
           and bool(str(gen).strip()) and (args.generation is None or str(gen) == args.generation), gen)
        cfg = doc["config"]
        ck("配置符合指定模式/EPG 开关", cfg.get("output_mode") == m.get("output_mode") == args.mode
           and cfg.get("epg_enable") is m.get("epg_enabled") is epg_on)
        return doc

    def expect_http(path, expected):
        try:
            get(path, raw=True)
        except HTTPError as exc:
            return exc.code == expected
        return False

    print(f"target={base} version={args.version} mode={args.mode} epg={args.epg}")
    try:
        st = status()
        meta, facets = st["meta"], st["facets"]
        before = datetime.now(TZ8).timestamp()
        catalog = get("/api/channels?group=&q=")
        after = datetime.now(TZ8).timestamp()
        channels = catalog["channels"]
        by_id = {str(c["uuid"]): c for c in channels}
        groups = dict(Counter(c["group"] for c in channels))
        order = catalog["order"]
        ck("频道数、ID、分组与状态一致", catalog.get("ok") is True and bool(channels)
           and len(channels) == len(by_id) == catalog["total"] == meta["channels"]
           and groups == catalog["groups"] == facets["top"] and catalog.get("generation") == meta["generation"], len(channels))
        ck("二次分组顺序与状态一致，4K 置顶，无旧分组", order == facets["order"]
           and order[:1] == ["4K"] and len(order) == len(set(order))
           and not {"超高清", "其他"}.intersection(groups))
        uhd = [c for c in channels if c["group"] == "4K"]
        ck("4K 组与状态一致、无同流重复", len(uhd) == meta["uhd"] == facets["g_uhd"]
           and len(uhd) == len({c["live"] for c in uhd}))
        cctv = [re.match(r"CCTV[- ]?(\d+)", re.sub(r"(4K|高清臻享|UHD|超高清)$", "", c["name"], flags=re.I).strip(), re.I)
                for c in uhd]
        nums = [int(m[1]) for m in cctv if m]
        ck("4K 组 CCTV 在最前且数字升序", all(cctv[:len(nums)]) and nums == sorted(nums), nums)
        uhd_data = get("/4k.json")["channels"]
        uhd_fields = ("uuid", "name", "live", "codec", "w", "h", "zhx")
        ck("4k.json 与 4K 组实测字段/真 4K 标记一致", len(uhd_data) == len(uhd) and all(
            all(x.get(k) == c.get(k) for k in uhd_fields) and x.get("real_4k") is c.get("is_4k")
            and (not c.get("is_4k") or (c.get("w", 0) >= 3840 and c.get("h", 0) >= 2160 and c.get("codec")))
            for x, c in zip(uhd_data, uhd)))
        catchup = [c for c in channels if c.get("catchup")]
        ck("回看频道数与状态一致", bool(catchup) and len(catchup) == meta["with_catchup"])
        main_list = {(c["group"], str(c["uuid"]), c["live"]): bool(c["catchup"]) for c in channels}
        for path, tokens in (("/iptv.m3u8", AP), ("/fongmi.m3u", AP), ("/kodi.m3u", KODI)):
            playlist(path, main_list, tokens, meta["backup_days"])

        orig = get("/orig.json")
        orig_list = {(g["name"], str(c["uuid"]), c["live"]): bool(c["back"])
                     for g in orig["groups"] for c in g["channels"]}
        orig_ids = {uid for _, uid, _ in orig_list}
        ck("原版分组、条目和唯一频道数自洽（允许跨组重复）", bool(orig_list)
           and len(orig["groups"]) == len({g["assortId"] for g in orig["groups"]})
           == len({g["name"] for g in orig["groups"]})
           and all(g["count"] == len(g["channels"]) for g in orig["groups"])
           and sum(g["count"] for g in orig["groups"]) == len(orig_list) == orig["total_entries"]
           and orig_ids.issubset(by_id) and len(orig_ids) == orig["unique_channels"])
        ck("原版 CLI/界面数据与频道标识、地址和回看能力一致", all(
            live == by_id[uid]["live"] and supported == bool(by_id[uid]["catchup"])
            for (_, uid, live), supported in orig_list.items()))
        playlist("/orig.m3u8", orig_list, AP, meta["backup_days"])
        mine = get("/mine.json")
        mine_list = {(g["name"], str(c["uuid"]), c["live"]): bool(c["back"])
                     for g in mine["groups"] for c in g["channels"]}
        ck("自定义清单与主频道 ID、直播和回看能力一致（允许跨组重复）",
           sum(len(g["channels"]) for g in mine["groups"]) == len(mine_list) == mine["total_entries"]
           and all(uid in by_id and live == by_id[uid]["live"] and supported == bool(by_id[uid]["catchup"])
                   for (_, uid, live), supported in mine_list.items()))
        for path, tokens in (("/mine.m3u8", AP), ("/mine_kodi.m3u", KODI), ("/mine_fongmi.m3u", AP)):
            playlist(path, mine_list, tokens, meta["backup_days"])
        main_epg = xmltv("/epg.xml", set(by_id))
        xmltv("/orig_epg.xml", orig_ids)
        ck("XMLTV 节目数与 meta.epg_programs 一致", len(main_epg) == meta["epg_programs"], len(main_epg))

        fongmi = get("/fongmi.json")
        tvbox = get("/tvbox.json")
        day_template = "/epg/{id}/{date}.json"
        fongmi_paths = ("/fongmi.m3u", "/orig.m3u8", "/mine_fongmi.m3u")
        ck("FongMi 三套直播入口及顺序正确", len(fongmi["lives"]) == len(fongmi_paths)
           and all(item.get("type") == 0 and serves(item.get("url"), path)
                   for item, path in zip(fongmi["lives"], fongmi_paths)))
        for name, doc, list_path, epg_path in (
            ("FongMi", fongmi, "/fongmi.m3u", day_template), ("TVBox", tvbox, "/iptv.m3u8", "/epg.xml"),
        ):
            lives = doc["lives"]
            primary = next((item for item in lives if serves(item.get("url"), list_path)), {})
            ck(f"{name} JSON 直播入口", primary.get("type") == 0 and all(absolute_http(x.get("url")) for x in lives))
            if epg_on:
                ck(f"{name} JSON 节目单入口", any(serves(url, epg_path) for url in doc["epg"])
                   and (serves(primary.get("epg"), epg_path) if name == "FongMi" else serves(doc.get("epg_url"), epg_path)))
            else:
                ck(f"{name} JSON 不广播 EPG 地址", not any({"epg", "epg_url"}.intersection(x) for x in [doc, *lives]))
        if args.mode == "full":
            ck("完整模式额外提供 live.txt", bool(get("/live.txt", raw=True).strip()))
        else:
            ck("精简模式不保留完整模式的 live.txt", expect_http("/live.txt", 404))

        now = datetime.now(TZ8)
        lo = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hi = lo + timedelta(days=1)
        live_now = [c["now"] for c in channels if c.get("now")]
        # GET 往返可能恰逢换档；用请求前后时刻而不是频道数量来判定。
        now_channel = float(now.timestamp())
        ck("now 数据与 EPG 开关一致，且覆盖请求时段", not live_now if not epg_on else all(
            n["s"] <= after and before < n["e"] and n["s"] < n["e"] and n["t"] for n in live_now))
        if epg_on:
            # 根据 XMLTV 当天节目挑样本，并与实际按天接口对比。
            today = [(p.get("channel"), program_time(p.attrib["start"]), program_time(p.attrib["stop"]), p.findtext("title"))
                     for p in main_epg if program_time(p.attrib["start"]) < hi and program_time(p.attrib["stop"]) > lo]
            uid = next((uid for uid, *_ in today if by_id[uid].get("catchup")), None)
            if not ck("找到当天有节目且支持回看的频道", uid is not None):
                raise ValueError("无法验证 FongMi 按天节目单和实际节目时段")
        else:
            uid = str(catchup[0]["uuid"])
            today = []
        day = lo.date().isoformat()
        body = get(f"/epg/{quote(uid, safe='')}/{day}.json")
        expected_day = {(s.astimezone(TZ8).strftime("%H:%M"), e.astimezone(TZ8).strftime("%H:%M"), title)
                        for key, s, e, title in today if key == uid}
        actual_day = {(p["start"], p["end"], p["title"]) for p in body["epg_data"]}
        ck("FongMi 当天 JSON 日期/时段/标题与 XMLTV 一致", body["date"] == day and actual_day == expected_day,
           len(body["epg_data"]))
        absent_day = (lo - timedelta(days=1)).date().isoformat()
        absent = get(f"/epg/{quote(uid, safe='')}/{absent_day}.json")
        ck("FongMi 非生成日期返回带日期的空表", absent == {"date": absent_day, "epg_data": []})
        e = get("/api/epg?" + urlencode({"uuid": uid}))
        programs = e["programs"]
        ck("按频道 API 结构 / EPG 开关", e.get("ok") is True and e.get("uuid") == uid
           and e.get("count") == len(programs) and bool(programs) == epg_on)
        past = [p for p in programs if p["end"] <= now_channel and 0 < now_channel - p["start"] < meta["backup_days"] * 86400]
        if epg_on and not ck("找到回看有效期内的已播节目", bool(past)):
            raise ValueError("未找到已播节目，不能用虚构节目替代")
        start, end = (lambda p: (p["start"], p["end"]))(max(past, key=lambda p: p["end"])) if past else (int(now_channel) - 7200, int(now_channel) - 3600)
        result = get("/api/catchup?" + urlencode({"uuid": uid, "start": start, "end": end}))
        times = [datetime.fromtimestamp(t, TZ8).strftime("%Y%m%d%H%M%S") for t in (start, end)]
        result_url = result.get("url", "")
        ck("回看接口生成正确时区和独立 lookback 路径（不等于可播）", result.get("ok") is True
           and absolute_http(result_url) and "/lookback/" in urlsplit(result_url).path
           and parse_qs(urlsplit(result_url).query).get("playseek") == ["-".join(times)]
           and [result.get("from"), result.get("to")] == times)
        ck("非媒体主机被拒绝（403）", expect_http("/api/spec?" + urlencode({"u": "http://fixture.invalid/test.m3u8"}), 403))
        final = get("/api/status")
        ck("验收期间实例版本、产物代号与配置未变且未在更新",
           final.get("version") == st.get("version") and final["meta"].get("generation") == meta.get("generation")
           and final.get("config") == st.get("config") and not final.get("updating", {}).get("running")
           and not final.get("updating", {}).get("error"))
    except Exception as exc:
        ck("验收未完成", False, exc)
    print(f"\n=== 第一轮：{len(passed)} 通过 / {len(failed)} 失败")
    if failed:
        print("失败项:", "; ".join(failed))
    return bool(failed)


if __name__ == "__main__":
    sys.exit(main())
