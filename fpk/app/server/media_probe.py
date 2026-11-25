# -*- coding: utf-8 -*-
"""从 MPEG-TS 直播流中解析真实媒体规格，不依赖 ffprobe。"""
import re
import urllib.request

UA = "okhttp/3.15.0"
AUDIO_SR = [96000, 88200, 64000, 48000, 44100, 32000, 24000,
            22050, 16000, 12000, 11025, 8000, 7350]


def _unescape(b):
    """去掉 H.264/HEVC 的防竞争字节 00 00 03 -> 00 00"""
    out = bytearray()
    i = 0
    n = len(b)
    while i < n:
        if i + 2 < n and b[i] == 0 and b[i + 1] == 0 and b[i + 2] == 3:
            out += b[i:i + 2]
            i += 3
        else:
            out.append(b[i])
            i += 1
    return bytes(out)


class BR:
    """位读取器。"""
    def __init__(self, d):
        self.d = d
        self.p = 0

    def bit(self):
        if self.p >> 3 >= len(self.d):
            self.p += 1
            return 0
        v = (self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1
        self.p += 1
        return v

    def bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ue(self):
        z = 0
        while self.p < len(self.d) * 8:
            if self.bit():
                break
            z += 1
            if z > 32:
                return 0
        if z == 0:
            return 0
        return (1 << z) - 1 + self.bits(z)

    def se(self):
        v = self.ue()
        return (v + 1) // 2 if v & 1 else -(v // 2)


def parse_h264_sps(nal):
    """nal: 含 1 字节 NAL 头的完整 SPS。返回 dict 或 None。"""
    try:
        d = _unescape(nal[1:])
        r = BR(d)
        profile = r.bits(8)
        r.bits(8)                      # constraint flags + reserved
        level = r.bits(8)
        r.ue()                         # sps_id
        chroma = 1
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma = r.ue()
            if chroma == 3:
                r.bit()                # separate_colour_plane
            r.ue()                     # bit_depth_luma_minus8
            r.ue()                     # bit_depth_chroma_minus8
            r.bit()                    # qpprime
            if r.bit():                # seq_scaling_matrix_present
                cnt = 12 if chroma == 3 else 8
                for i in range(cnt):
                    if r.bit():
                        size = 16 if i < 6 else 64
                        last = nxt = 8
                        for _ in range(size):
                            if nxt != 0:
                                nxt = (last + r.se() + 256) % 256
                            if nxt != 0:
                                last = nxt
        r.ue()                         # log2_max_frame_num_minus4
        poc = r.ue()
        if poc == 0:
            r.ue()                     # log2_max_pic_order_cnt_lsb_minus4
        elif poc == 1:
            r.bit()
            r.se()
            r.se()
            for _ in range(min(r.ue(), 64)):
                r.se()
        r.ue()                         # max_num_ref_frames
        r.bit()                        # gaps_in_frame_num
        w_mbs = r.ue() + 1
        h_map = r.ue() + 1
        frame_only = r.bit()
        if not frame_only:
            r.bit()                    # mb_adaptive_frame_field
        r.bit()                        # direct_8x8_inference
        cl = cr = ct = cb = 0
        if r.bit():                    # frame_cropping
            cl = r.ue()
            cr = r.ue()
            ct = r.ue()
            cb = r.ue()

        sub_w = 2 if chroma in (1, 2) else 1
        sub_h = 2 if chroma == 1 else 1
        w = w_mbs * 16
        h = (2 - frame_only) * h_map * 16
        w -= (cl + cr) * sub_w
        h -= (ct + cb) * sub_h * (2 - frame_only)

        fps = None
        sar = None
        if r.bit():                    # vui_parameters_present
            if r.bit():                # aspect_ratio_info_present
                idc = r.bits(8)
                if idc == 255:
                    sw = r.bits(16)
                    sh = r.bits(16)
                    if sw and sh:
                        sar = "%d:%d" % (sw, sh)
                else:
                    sar = {1: "1:1", 2: "12:11", 3: "10:11", 4: "16:11", 5: "40:33",
                           6: "24:11", 7: "20:11", 8: "32:11", 9: "80:33",
                           10: "18:11", 11: "15:11", 12: "64:33", 13: "160:99",
                           14: "4:3", 15: "3:2", 16: "2:1"}.get(idc)
            if r.bit():                # overscan_info
                r.bit()
            if r.bit():                # video_signal_type
                r.bits(3)
                r.bit()
                if r.bit():            # colour_description
                    r.bits(8); r.bits(8); r.bits(8)
            if r.bit():                # chroma_loc_info
                r.ue(); r.ue()
            if r.bit():                # timing_info
                nut = r.bits(32)
                ts = r.bits(32)
                r.bit()                # fixed_frame_rate
                if nut:
                    v = ts / (2.0 * nut)
                    fps = round(v, 2)
        return {"w": w, "h": h, "codec": "H.264",
                "profile": {66: "Baseline", 77: "Main", 100: "High", 110: "High 10",
                            122: "High 4:2:2", 244: "High 4:4:4"}.get(profile, "P%d" % profile),
                "level": "%d.%d" % (level // 10, level % 10),
                "fps": fps, "sar": sar}
    except Exception:
        return None


def parse_h265_sps(nal):
    """nal: 含 2 字节 NAL 头的 HEVC SPS。取宽高与档次。

    注意 profile_tier_level 里 general_level_idc 是 u(8)，不是 ue ——
    用 ue 会让后面所有字段整体错位（曾解出 1x0、9x1920 这种荒谬宽高）。
    """
    try:
        d = _unescape(nal[2:])
        r = BR(d)
        r.bits(4)                      # sps_video_parameter_set_id
        max_sub = r.bits(3)            # sps_max_sub_layers_minus1
        r.bits(1)                      # sps_temporal_id_nesting_flag

        # ---- profile_tier_level
        r.bits(2)                      # general_profile_space
        tier = r.bits(1)               # general_tier_flag
        profile_idc = r.bits(5)        # general_profile_idc
        r.bits(32)                     # general_profile_compatibility_flag[32]
        r.bits(48)                     # progressive/interlaced/non_packed/frame_only + 43 reserved
        level_idc = r.bits(8)          # general_level_idc  ← u(8)
        sub_profile = [0] * max_sub
        sub_level = [0] * max_sub
        if max_sub > 0:
            for i in range(max_sub):
                sub_profile[i] = r.bits(1)
                sub_level[i] = r.bits(1)
            for _ in range(max_sub, 8):     # reserved_zero_2bits
                r.bits(2)
            for i in range(max_sub):
                if sub_profile[i]:
                    r.bits(2); r.bits(1); r.bits(5); r.bits(32); r.bits(48)
                if sub_level[i]:
                    r.bits(8)

        r.ue()                          # sps_seq_parameter_set_id
        chroma = r.ue()                 # chroma_format_idc
        if chroma == 3:
            r.bit()                     # separate_colour_plane_flag
        w = r.ue()                      # pic_width_in_luma_samples
        h = r.ue()                      # pic_height_in_luma_samples
        if r.bit():                     # conformance_window_flag
            cl = r.ue(); cr = r.ue(); ct = r.ue(); cb = r.ue()
            sub_w = 2 if chroma in (1, 2) else 1
            sub_h = 2 if chroma == 1 else 1
            w -= (cl + cr) * sub_w
            h -= (ct + cb) * sub_h
        if not (0 < w < 20000 and 0 < h < 20000):
            return None
        # ---- 继续跑到 VUI 拿帧率（HEVC 的 VUI 结构比 H.264 更绕，字段顺序不同）
        fps = None
        try:
            r.ue()                      # bit_depth_luma_minus8
            r.ue()                      # bit_depth_chroma_minus8
            r.ue()                      # log2_max_pic_order_cnt_lsb_minus4
            sub_ordering = r.bit()      # sps_sub_layer_ordering_info_present_flag
            for _ in range(max_sub if sub_ordering else max_sub + 1):
                r.ue(); r.ue(); r.ue()  # max_dec_pic_buffering / num_reorder / max_latency
            r.ue(); r.ue(); r.ue(); r.ue(); r.ue(); r.ue()   # 6 个 ue：log2_min_cb .. log2_diff_max_min_transform
            r.ue()                      # max_transform_hierarchy_depth_inter
            r.ue()                      # max_transform_hierarchy_depth_intra
            if r.bit():                 # scaling_list_enabled
                if r.bit():             # sps_scaling_list_data_present
                    for size_id in range(4):
                        for matrix_id in range(6 if size_id == 3 else 3):
                            if not r.bit():
                                continue
                            coef_num = min(64, 1 << (4 + (size_id << 1)))
                            if size_id > 1:
                                r.se()          # dc_coef_minus8
                            for _ in range(coef_num):
                                r.se()
            r.bit()                     # amp_enabled
            if r.bit():                 # sample_adaptive_offset
                r.bit()
            if r.bit():                 # pcm_enabled
                r.bits(4); r.bits(4)
                r.ue(); r.ue()
                r.bit()
            num_st = r.ue()             # num_short_term_ref_pic_sets
            for i in range(min(num_st, 64)):
                # 简化跳过：inter_ref_pic_set_prediction_flag + 各自字段
                if i != 0 and r.bit():
                    r.bit(); r.ue(); r.ue()
                    for _ in range(min(r.ue(), 32)):
                        r.bits(1); r.bits(1) if False else None
                    continue
                cnt = r.ue()
                for _ in range(min(cnt, 32) + 1):
                    r.ue(); r.bits(1)
            if r.bit():                 # long_term_ref_pics
                for _ in range(min(r.ue(), 32)):
                    r.bits(16); r.bits(1)
            if r.bit():                 # sps_temporal_mvp_enabled
                pass
            r.bit()                     # strong_intra_smoothing
            if r.bit():                 # vui_parameters_present
                if r.bit():             # aspect_ratio_info
                    idc = r.bits(8)
                    if idc == 255:
                        r.bits(16); r.bits(16)
                if r.bit():             # overscan
                    r.bit()
                if r.bit():             # video_signal_type
                    r.bits(3); r.bit()
                    if r.bit():
                        r.bits(24)
                if r.bit():             # chroma_loc
                    r.ue(); r.ue()
                if r.bit():             # neutral_chroma
                    r.bits(8); r.bits(8)
                if r.bit():             # field_seq
                    r.bit()
                frame_field = r.bit()   # frame_field_info_present_flag（无子字段）
                default_display = r.bit()
                if default_display:
                    r.ue(); r.ue(); r.ue(); r.ue()
                if r.bit():             # vui_timing_info_present
                    num_units = r.bits(32)
                    time_scale = r.bits(32)
                    if num_units:
                        fps = round(time_scale / num_units, 2)
        except Exception:
            pass
        return {"w": w, "h": h, "codec": "H.265",
                "profile": {1: "Main", 2: "Main 10", 3: "Main Still Picture"}.get(profile_idc,
                                                                                   "Profile %d" % profile_idc),
                "level": "%d.%d" % (level_idc // 30, (level_idc % 30) // 3),
                "fps": fps, "sar": None,
                "tier": "High" if tier else "Main"}
    except Exception:
        return None


def parse_adts(b):
    """在数据里找 ADTS 同步头。

    裸 AAC 流里随机字节常伪装成 FF F?（实测干扰项出现三百多次），单看第一个
    命中会解错（曾把 48kHz/2ch 解成 64kHz/1ch、32kHz/3ch）。可靠做法：
    只认 layer==0 的合法头，全局统计 (采样率,声道) 出现次数，取压倒性多数。
    """
    n = len(b)
    votes = {}
    for i in range(n - 7):
        if b[i] != 0xFF or (b[i + 1] & 0xF0) != 0xF0:
            continue
        if (b[i + 1] >> 1) & 0x03:          # ADTS 规定 layer 必须为 0
            continue
        idx = (b[i + 2] >> 2) & 0x0F
        ch = ((b[i + 2] & 0x01) << 2) | ((b[i + 3] >> 6) & 0x03)
        if idx >= len(AUDIO_SR) or ch == 0:
            continue
        flen = ((b[i + 3] & 0x03) << 11) | (b[i + 4] << 3) | ((b[i + 5] >> 5) & 0x07)
        if flen < 7 or flen > 8191:
            continue
        k = (AUDIO_SR[idx], ch)
        votes[k] = votes.get(k, 0) + 1
    if not votes:
        return None
    (sr, ch), cnt = max(votes.items(), key=lambda kv: kv[1])
    if cnt < 3:                              # 票数太少，不足以确认为真同步头
        return None
    return {"sr": sr, "ch": ch, "codec": "AAC"}


def parse_ac3(b):
    """找 AC-3 同步字 0x0B77。

    0B77 只有两字节，裸流里误命中率很高（实测把 AAC 流误判成 AC-3），
    因此要求采样率码合法、且能按帧长连续走出至少 3 帧。
    """
    n = len(b)
    for i in range(min(n - 8, 60000)):
        if b[i] != 0x0B or b[i + 1] != 0x77:
            continue
        p = i
        chain = 0
        first = None
        while p + 8 <= n and chain < 4:
            if b[p] != 0x0B or b[p + 1] != 0x77:
                break
            fscod = (b[p + 4] >> 6) & 0x03
            frmsizecod = b[p + 4] & 0x3F
            acmod = (b[p + 6] >> 1) & 0x07
            if fscod >= 3 or frmsizecod >= 38:
                break
            sr = (48000, 44100, 32000)[fscod]
            ch = (2, 1, 2, 3, 3, 4, 4, 5)[acmod]
            if first is None:
                first = (sr, ch)
            elif first != (sr, ch):
                break
            # AC-3 帧长表（words）：以 48k/44.1k/32k 三组分开
            frame_bytes = _ac3_frame_bytes(fscod, frmsizecod)
            if not frame_bytes or p + frame_bytes > n:
                break
            chain += 1
            p += frame_bytes
        if chain >= 3:
            return {"sr": first[0], "ch": first[1], "codec": "AC-3"}
    return None


_AC3_WORDS = [
    64, 64, 80, 80, 96, 96, 112, 112, 128, 128, 160, 160, 192, 192, 224, 224,
    256, 256, 320, 320, 384, 384, 448, 448, 512, 512, 640, 640, 768, 768,
    896, 896, 1024, 1024, 1152, 1152, 1280, 1280,
]


def _ac3_frame_bytes(fscod, frmsizecod):
    if frmsizecod >= len(_AC3_WORDS):
        return 0
    words = _AC3_WORDS[frmsizecod]
    if fscod == 0:
        return words * 2
    if fscod == 1:
        return int(words * 2 * 48000 / 44100) & ~1
    return words * 2


def extract_pes(ts):
    """从 TS 中拼出各 PID 的 PES 载荷片段。返回 {pid: bytes}"""
    pid_buf = {}
    sid_map = {}
    n = len(ts)
    for off in range(0, n - 187, 188):
        if ts[off] != 0x47:
            continue
        pusi = ts[off + 1] & 0x40
        pid = ((ts[off + 1] & 0x1F) << 8) | ts[off + 2]
        afc = (ts[off + 3] >> 4) & 0x03
        p = off + 4
        if afc & 0x02:
            afl = ts[off + 4]
            p += 1 + afl
        if not (afc & 0x01) or p >= off + 188:
            continue
        seg = ts[p:off + 188]
        # PUSI 起点处含 PES 头（00 00 01 sid len flags hdrlen ...）。
        # 必须剥掉，否则 PES 头字节会插进音频/视频裸流中间，把 ADTS 帧链打断。
        # 但 stream_id 得单独记下来 —— 剥掉之后就再也分不出哪条是音轨了。
        if pusi and len(seg) > 9 and seg[0] == 0 and seg[1] == 0 and seg[2] == 1:
            sid_map[pid] = seg[3]
            hdr_len = seg[8]
            seg = seg[9 + hdr_len:]
        buf = pid_buf.setdefault(pid, bytearray())
        if len(buf) < 400000:
            buf += seg
    return {k: (bytes(v), sid_map.get(k, -1)) for k, v in pid_buf.items()}


def find_nals(data, hevc=False):
    """在 Annex-B 字节流中找出 SPS/VPS 等 NAL。"""
    out = []
    i = 0
    n = len(data)
    while i < n - 4:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            start = i + 3
            j = start
            while j < n - 3:
                if data[j] == 0 and data[j + 1] == 0 and (data[j + 2] == 1 or data[j + 2] == 0):
                    break
                j += 1
            out.append(data[start:j])
            i = j
        else:
            i += 1
    return out


def probe_url(m3u8_url, timeout=8, max_bytes=None):
    """探测一路直播流。

    max_bytes: 只取切片前 N 字节。4K 单切片可达 20~45MB，
    而 SPS/ADTS 头都在开头几十 KB，读整段既慢又占内存。
    """
    import urllib.parse
    req = urllib.request.Request(m3u8_url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
        base = r.geturl()
    segs = []
    dur = None
    last = None
    for line in body.splitlines():
        t = line.strip()
        if t.startswith("#EXTINF:"):
            try:
                last = float(t.split(":")[1].split(",")[0])
            except Exception:
                last = None
        elif t and not t.startswith("#"):
            segs.append((urllib.parse.urljoin(base, t), last))
    if not segs:
        raise RuntimeError("播放列表里没有切片")
    # 取倒数第二个（最后一个可能还在写入）
    seg_url, seg_dur = segs[-2] if len(segs) >= 2 else segs[-1]
    h = {"User-Agent": UA}
    if max_bytes:
        h["Range"] = "bytes=0-%d" % (max_bytes - 1)
    req = urllib.request.Request(seg_url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ts = r.read(max_bytes) if max_bytes else r.read()
    info = {"bytes": len(ts), "seg_dur": seg_dur}
    if seg_dur:
        info["bitrate"] = int(len(ts) * 8 / seg_dur)

    pes = extract_pes(ts)
    video = None
    audio = None
    # 只在真正的音频轨上找音频头。若在所有 PID 上盲试，视频/无关 PID 里的
    # 随机字节会伪造出更多假 ADTS 头，票数反而压过真音频轨（实测如此）。
    for pid, (buf, sid) in pes.items():
        is_video = 0xE0 <= sid <= 0xEF
        is_audio = 0xC0 <= sid <= 0xDF
        if is_video and not video:
            for nal in find_nals(buf):
                if not nal:
                    continue
                if (nal[0] & 0x1F) == 7:
                    video = parse_h264_sps(nal)
                    if video:
                        break
                if len(nal) >= 2 and ((nal[0] >> 1) & 0x3F) == 33:
                    video = parse_h265_sps(nal)
                    if video:
                        break
        if is_audio and not audio:
            audio = parse_adts(buf) or parse_ac3(buf)
        if video and audio:
            break
    info["video"] = video
    info["audio"] = audio
    return info