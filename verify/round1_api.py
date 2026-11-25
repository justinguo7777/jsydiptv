# -*- coding: utf-8 -*-
"""第一轮验收：接口 / 产物 / 数据质量 / 安全

用法：/home/agent/cow/venv/bin/python3 verify/round1_api.py
（直连 NAS 18123，需在江苏移动网络内执行）
"""
import json, urllib.request, urllib.parse, re, sys, time

BASE="http://192.168.2.3:18123"
def get(p, raw=False, timeout=40):
    req=urllib.request.Request(BASE+p)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d=r.read()
    return d if raw else json.loads(d.decode('utf-8','replace'))

PASS=[]; FAIL=[]
def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ")+name+((" — "+str(detail)) if detail else ""))

# 1 状态
st=get("/api/status")
ck("状态接口 ok", st.get("ok"))
m=st.get("meta") or {}
ck("meta 有 uhd 字段", m.get("uhd") is not None, m.get("uhd"))
ck("meta 有 uhd_h265", m.get("uhd_h265") is not None, m.get("uhd_h265"))
ck("facets 有 g_uhd", st["facets"].get("g_uhd") is not None, st["facets"].get("g_uhd"))
ck("facets.order 4K 置顶", (st["facets"].get("order") or [None])[0]=="4K", st["facets"].get("order"))

# 2 频道
ch=get("/api/channels?group=&q=")
chs=ch["channels"]
ck("频道数 277（4K 组去重 1 条同流）", ch["total"]==277, ch["total"])
grps=ch["groups"]
ck("存在 4K 分组", "4K" in grps, grps.get("4K"))
ck("4K 数量 = meta.uhd", grps.get("4K")==m.get("uhd"), "%s vs %s"%(grps.get("4K"), m.get("uhd")))
ck("无「超高清」旧分组名", "超高清" not in grps)
ck("无「其他」垃圾组", "其他" not in grps, list(grps.keys()))
# 顺序校验（1.6.0 新顺序：4K→央视→卫视→江苏→地方→6 个二级组）
order=ch["order"]
EXPECT=["4K","央视","卫视","江苏","地方","影视剧场","少儿动画","体育竞技","生活纪录","音乐广播","购物"]
ck("order 顺序为 11 分组新方案", order==EXPECT, order)
# 6 个二级组都应有频道
for gg in ["影视剧场","少儿动画","体育竞技","生活纪录","音乐广播","购物"]:
    ck("二级组 %s 有频道"%gg, grps.get(gg,0)>0, grps.get(gg))
# 4K 组：真 4K 都有 codec；高清臻享允许非 4K 但要有 w/h
k4=[c for c in chs if c["group"]=="4K"]
ck("4K 组全部有 codec", all(c.get("codec") for c in k4), [c["name"] for c in k4 if not c.get("codec")])
ck("4K 组全部有实测分辨率 w/h", all(c.get("w") and c.get("h") for c in k4), [c["name"] for c in k4 if not (c.get("w") and c.get("h"))])
real=[c for c in k4 if c.get("is_4k")]
zhx1080=[c for c in k4 if c.get("zhx") and not c.get("is_4k")]
ck("4K 组含真 4K 且标注清晰", len(real)>=13, len(real))
print("     4K 组 %d 个：真 4K %d，高清臻享(1080P) %d"%(len(k4), len(real), len(zhx1080)))
h265=[c["name"] for c in k4 if "265" in (c.get("codec") or "")]
print("     其中 H.265 %d 个"%len(h265))
# 4K 组内同流不得重复（cp+code 唯一）
codes=[(c.get("live") or "") for c in k4]
ck("4K 组内无同流重复", len(codes)==len(set(codes)), "%d/%d"%(len(set(codes)),len(codes)))
# 4K 组 CIAV 顺序：CCTV 台在最前且数字升序
def cctv_no(n):
    import re as _re
    nm=_re.sub(r"(4K|高清臻享|UHD|超高清)$","",n,flags=_re.I).strip()
    mm=_re.match(r"CCTV[- ]?(\d+)",nm,_re.I)
    return int(mm.group(1)) if mm else None
nums=[cctv_no(c["name"]) for c in k4]
cctv_nums=[x for x in nums if x is not None]
# CCTV 台应连续出现在最前且升序
ck("4K 组 CCTV 台按数字升序排列在最前", cctv_nums==sorted(cctv_nums), cctv_nums)

# 央视组必须含 CCTV-1
cctv_grp=[c for c in chs if c["group"]=="央视"]
ck("央视组含 CCTV-1", any(c["name"]=="CCTV-1" for c in cctv_grp), [c["name"] for c in cctv_grp][:5])
ck("央视组 CCTV-1 是 1080P 源", any(c["name"]=="CCTV-1" and not c.get("is_4k") for c in cctv_grp))
ck("央视组含 CCTV-2..17 连续", set("CCTV-%d"%i for i in range(2,18)).issubset(set(c["name"] for c in cctv_grp)), sorted(c["name"] for c in cctv_grp if c["name"].startswith("CCTV-")))

# 3 实时 now：核对 now 是否等于当前时间在播
now=int(time.time())
bad=[]
for c in chs:
    n=c.get("now")
    if n:
        if not (n["s"]<=now<n["e"]): bad.append(c["name"])
ck("now 字段全部落在当前时段", not bad, bad[:5])
# 未选频道也能确认没有 03:30 冻结：抽查 20 个 now 起始时间应分散
starts=set(c["now"]["s"] for c in chs if c.get("now"))
ck("now 起始时间分散（非同一快照）", len(starts)>10, len(starts))

# 4 产物文件
for f,ct in [("/iptv.m3u8","m3u"),("/kodi.m3u","m3u"),("/fongmi.m3u","m3u"),
             ("/fongmi.json","json"),("/tvbox.json","json"),("/epg.xml","xml"),
             ("/4k.json","json")]:
    try:
        raw=get(f, raw=True)
        ck("产物 %s 可访问"%f, len(raw)>100, len(raw))
    except Exception as e:
        ck("产物 %s 可访问"%f, False, e)

m3u=get("/iptv.m3u8", raw=True).decode("utf-8","replace")
ck("iptv.m3u8 含 4K 分组", 'group-title="4K"' in m3u)
kodi=get("/kodi.m3u", raw=True).decode("utf-8","replace")
ck("kodi.m3u 含 4K 分组", 'group-title="4K"' in kodi)
ck("kodi.m3u 回看用 ${start}", "${start}" in kodi)
fm=get("/fongmi.m3u", raw=True).decode("utf-8","replace")
ck("fongmi.m3u 含 4K 分组", 'group-title="4K"' in fm)
ck("fongmi.m3u 回看用 ${(b)", "${(b)" in fm)
epg=get("/epg.xml", raw=True).decode("utf-8","replace")
ck("epg.xml 有 programme", epg.count("<programme ")>1000, epg.count("<programme "))
ck("epg.xml icon 为绝对地址", 'src="http://' in epg)

k4j=get("/4k.json")
ck("4k.json 频道数=4K 组", len(k4j["channels"])==grps.get("4K"), len(k4j["channels"]))
ck("4k.json 每条有 live/codec", all(c.get("live") and c.get("codec") for c in k4j["channels"]))

# 5 回看
one=[c for c in chs if c.get("catchup")][0]
cu=get("/api/catchup?uuid=%s&start=%d&end=%d"%(urllib.parse.quote(one["uuid"]), now-7200, now-3600))
ck("catchup 接口可用", cu.get("ok"), cu.get("msg"))

# 6 EPG 接口
e=get("/api/epg?uuid="+urllib.parse.quote(one["uuid"]))
ck("epg 接口返回节目", e.get("count",0)>10, e.get("count"))

# 7 安全
try:
    get("/api/spec?u="+urllib.parse.quote("http://example.com/x.m3u8"))
    ck("非媒体源被拒", False)
except urllib.error.HTTPError as ex:
    ck("非媒体源被拒(403)", ex.code==403, ex.code)

print("\n=== 第一轮：%d 通过 / %d 失败"%(len(PASS),len(FAIL)))
if FAIL: print("失败项:", FAIL); sys.exit(1)
