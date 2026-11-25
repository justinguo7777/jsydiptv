# 更新后回归验证清单

> 配套 `DEVELOPMENT-REPORT.md` 使用。
> 每次改动代码或重新打包，按 A→B→C 顺序逐项打勾；任一项失败不得发布。

---

## A. 打包前（源码层，本地检查）

```bash
cd /home/agent/cow/projects/IPTV
```

- [ ] **版本号已改且复核**
  ```bash
  grep ^version fpk/manifest
  # 必须是你这次的目标版本，不能沿用小版本
  ```

- [ ] **`wizard/install` 存在且含端口字段**
  ```bash
  ls fpk/wizard/            # 应同时有 config 和 install
  python3 -c "import json;d=json.load(open('fpk/wizard/install'));print([i.get('field') for i in d[0]['items']])"
  # 输出必须包含 wizard_port
  ```

- [ ] **入口配置端口语义正确**
  ```bash
  cat fpk/app/ui/config
  # port 用 ${wizard_port} 的前提：wizard/install 里有该字段（上面已验证）
  ```

- [ ] **脚本语法**
  ```bash
  for f in fpk/cmd/*; do bash -n "$f" || echo "语法错误: $f"; done
  ```

- [ ] **Python 语法**
  ```bash
  python3 -m py_compile fpk/app/server/server.py fpk/app/server/media_probe.py && echo OK
  ```

- [ ] **无硬编码卷路径**（必须为空输出）
  ```bash
  grep -rnE '/vol[0-9]|/usr/local/apps' fpk/ --include='*.py' --include='*.sh' \
    --include=main --include='*_callback' --include='*_init' || echo "✅ 无硬编码路径"
  ```

- [ ] **媒体主机白名单覆盖所需网段**
  ```bash
  sed -n '/def _is_media_host/,/^def /p' fpk/app/server/server.py | grep -E '223|221|112|120|121'
  ```

---

## B. 打包后（包结构层）

- [ ] **根层级正确**（4 个必须是根文件）
  ```bash
  tar tzf jsydiptv-<版本>.fpk | grep -vE '^(cmd|config|wizard)/'
  # 应含：ICON.PNG  ICON_256.PNG  app.tgz  manifest
  ```

- [ ] **wizard 双文件都在包里**
  ```bash
  tar tzf jsydiptv-<版本>.fpk | grep wizard
  # 应有 wizard/config 和 wizard/install
  ```

- [ ] **app.tgz 内容完整**
  ```bash
  mkdir -p /tmp/chk && cd /tmp/chk && tar xzf /path/jsydiptv-<版本>.fpk && tar tzf app.tgz | sort
  # 必须含：server/server.py  server/media_probe.py  www/index.html
  #          www/hls.min.js   ui/config  ui/images/icon_64.png  ui/images/icon_256.png
  ```

- [ ] **包与源码逐字节一致**
  ```bash
  cd /tmp/chk && mkdir -p src && tar xzf app.tgz -C src
  diff src/server/server.py      /home/agent/cow/projects/IPTV/fpk/app/server/server.py
  diff src/server/media_probe.py /home/agent/cow/projects/IPTV/fpk/app/server/media_probe.py
  diff src/www/index.html        /home/agent/cow/projects/IPTV/fpk/app/www/index.html
  diff src/ui/config             /home/agent/cow/projects/IPTV/fpk/app/ui/config
  diff -r cmd    /home/agent/cow/projects/IPTV/fpk/cmd
  diff -r wizard /home/agent/cow/projects/IPTV/fpk/wizard
  diff manifest  /home/agent/cow/projects/IPTV/fpk/manifest
  # 全部无输出 = 一致
  ```

- [ ] **权限规范**
  ```bash
  cd /tmp/chk && ls -l ICON.PNG ICON_256.PNG manifest   # 644
  ls -l cmd/                                            # 755
  ```

- [ ] **记录指纹**
  ```bash
  md5sum jsydiptv-<版本>.fpk
  ```

---

## C. 安装后（运行时，NAS 上）

### C.1 安装与启动

- [ ] **安装成功**
  ```bash
  appcenter-cli install-fpk /tmp/jsydiptv-<版本>.fpk --env /tmp/i.env
  # /tmp/i.env 内容：
  #   wizard_port=18123
  #   wizard_district=all
  ```

- [ ] **状态 running**
  ```bash
  appcenter-cli status jsydiptv      # running
  ```

- [ ] **端口在监听**
  ```bash
  ss -lnt | grep 18123
  ```

### C.2 入口正确性（空白页事故的关键检查）

- [ ] **entry 端口是真实数字**（最易复发，必查）
  ```bash
  sudo -u postgres psql -d trim_sac -tc \
    "SELECT url FROM entry WHERE app_name='jsydiptv';"
  # 期望：{"protocol":"http","port":"18123","path":"/"}
  # ❌ 若出现 "${wizard_port}" → 缺 wizard/install，立即回退
  ```

- [ ] **安装变量已入库**
  ```bash
  sudo -u postgres psql -d appcenter -c \
    "SELECT e.k,e.v FROM app a JOIN app_env e ON e.app_id=a.id WHERE a.app_name='jsydiptv';"
  # 应有 wizard_port / wizard_district 两条记录
  ```

### C.3 服务功能

- [ ] **首页正常**
  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' --noproxy '*' http://<NAS>:18123/
  curl -s --noproxy '*' http://<NAS>:18123/ | grep -o '<title>[^<]*</title>'
  # 期望：200 + <title>江苏移动 IPTV</title>
  ```

- [ ] **静态资源**
  ```bash
  curl -s -o /dev/null -w 'hls=%{http_code}\n' --noproxy '*' http://<NAS>:18123/hls.min.js
  ```

- [ ] **状态接口**
  ```bash
  curl -s --noproxy '*' http://<NAS>:18123/api/status | head -c 300
  # 应含 channels 数、group 分布、schedule.next_run
  ```

- [ ] **频道与节目单**
  ```bash
  curl -s --noproxy '*' 'http://<NAS>:18123/api/channels' | head -c 200
  curl -s --noproxy '*' 'http://<NAS>:18123/api/epg?uuid=<某uuid>' | head -c 200
  ```

- [ ] **流规格探测**
  ```bash
  curl -s --noproxy '*' "http://<NAS>:18123/api/spec?u=<URL编码的m3u8>"
  # 期望含 "codec":"H.264" 与 audio
  ```

- [ ] **白名单生效（防开放代理）**
  ```bash
  curl -s --noproxy '*' "http://<NAS>:18123/api/spec?u=http%3A%2F%2Fexample.com%2Fx.m3u8"
  # 期望 403 "只允许探测运营商媒体源"
  ```

### C.4 启停可靠性（端口被占事故的关键检查）

- [ ] **stop 真正释放**
  ```bash
  appcenter-cli stop jsydiptv
  sleep 4
  ss -lnt | grep 18123 && echo "❌ 仍占用" || echo "✅ 已释放"
  ps aux | grep -v grep | grep jsydiptv || echo "✅ 无进程"
  ```

- [ ] **反复启停 3 次都正常**
  ```bash
  for i in 1 2 3; do
    appcenter-cli start jsydiptv >/dev/null 2>&1; sleep 8
    ss -lnt | grep -q 18123 && echo "第$i次 start ✅" || echo "第$i次 start ❌"
    appcenter-cli stop jsydiptv  >/dev/null 2>&1; sleep 3
    ss -lnt | grep -q 18123 && echo "第$i次 stop ❌ 残留" || echo "第$i次 stop ✅"
  done
  ```

- [ ] **孤儿进程自愈**
  ```bash
  # 手工制造孤儿：删掉 pid 文件后启动
  rm -f /vol4/@appdata/jsydiptv/app.pid   # 路径按实际卷
  appcenter-cli start jsydiptv
  sleep 8
  ss -lnt | grep 18123 && echo "✅ 能自愈启动" || echo "❌ 自愈失败"
  ```

### C.5 卸载干净

- [ ] **无残留**
  ```bash
  appcenter-cli stop jsydiptv; appcenter-cli uninstall jsydiptv
  sleep 4
  ss -lnt | grep 18123 && echo "❌ 端口残留" || echo "✅ 端口释放"
  ps aux | grep -v grep | grep jsydiptv || echo "✅ 无进程"
  ls -d /vol*/@app*/*jsydiptv* /var/apps/jsydiptv 2>/dev/null || echo "✅ 无目录残留"
  # 若有 @apphome/@appmeta/@appshare 空壳，手工 rm -rf（会干扰重装）
  ```

---

## D. 数据质量抽查

- [ ] 频道数合理（全江苏约 278）
- [ ] 分组齐全（央视/卫视/江苏/地方-*/其他）
- [ ] `epg.xml` programme 条数与界面展示**一致**
- [ ] 随机抽 3 个频道验证直播地址 `200`
- [ ] 随机抽 1 个 `back=1` 频道验证回看地址可播
- [ ] 隐私扫描：`grep -rlE '1[3-9][0-9]{9}' var/data/*.m3u* var/data/*.json` 应为空

---

## E. 失败回退

任一项失败：

1. **立即卸载当前版本**（`appcenter-cli uninstall jsydiptv`）
2. 确认端口与进程干净（C.5）
3. 回退源码到上一通过版本，或从 `archive/` 取历史包对照排查
4. 对照 `DEVELOPMENT-REPORT.md` §8 事故档案定位
5. 修复后**从 A 重新走一遍**，不要跳步

> ⚠️ `archive/` 里的历史包仅用于对照分析，不要拿去安装在用环境。

---

*检查项编号与 `DEVELOPMENT-REPORT.md` 的 §13 一一对应。*