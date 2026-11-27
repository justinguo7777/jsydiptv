#!/bin/bash
# 打包 jsydiptv fpk —— 固化「A 打包前 → B 打包后」全部检查，一条命令出包。
#
# 用法：
#   tools/build.sh            # 用 fpk/manifest 里的 version 打包
#   tools/build.sh --check    # 只做检查，不出包
#
# 为什么要有这个脚本：
#   手工 tar 漏掉 ui/、app.tgz 多套一层 app/、清 __pycache__ 不彻底 ——
#   每个坑都在这项目上真实发生过（1.1.0 因此整包不可用）。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
FPK="fpk"

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
fail=0
ok()   { echo "${GRN}  ✓${RST} $*"; }
bad()  { echo "${RED}  ✗${RST} $*"; fail=$((fail+1)); }
warn() { echo "${YLW}  !${RST} $*"; }
step() { echo; echo "${DIM}── $* ──${RST}"; }

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# ── 可重现构建（Reproducible Build）──
# tar 默认把文件 mtime/uid/gid 写进包头 → 内容相同、mtime 不同就产出不同 MD5，
# 校验和随之失去「可比对」的意义。固定为源码提交时间戳后，同源码必得同包。
# 优先级：SOURCE_DATE_EPOCH > git 提交时间 > 固定值（保证永不回退到当前时间）
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH=$(git log -1 --format=%ct 2>/dev/null)
    [ -n "$SOURCE_DATE_EPOCH" ] || SOURCE_DATE_EPOCH=1700000000
fi
export SOURCE_DATE_EPOCH
TAR_DET=(--sort=name --mtime="@${SOURCE_DATE_EPOCH}" --owner=0 --group=0 --numeric-owner)

# 构建指纹：记录本次包由哪个 commit、什么工作区状态产出
BUILD_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
BUILD_DIRTY=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
export BUILD_COMMIT BUILD_DIRTY SOURCE_DATE_EPOCH

# ─────────────────────────── A. 打包前 ───────────────────────────
step "A. 打包前检查（源码层）"

VER=$(grep -E '^version=' "$FPK/manifest" | cut -d= -f2 | tr -d '[:space:]')
if ! [[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    bad "fpk/manifest 版本不符合 X.Y.Z 格式"
    VER="0.0.0"
else
    ok "manifest version=$VER"
fi

# 版本号是否已升（与最近 git tag 比对，仅提示，不阻断）
LASTTAG=$(git describe --tags --abbrev=0 2>/dev/null)
if [ -n "$LASTTAG" ]; then
    if [ "v$VER" = "$LASTTAG" ]; then
        warn "version=v$VER 与最近 tag（$LASTTAG）相同 —— 确认是否忘了升版本号"
    else
        ok "版本 v$VER（最近本地 tag：$LASTTAG）"
    fi
fi

# wizard 双文件 + 端口字段
for w in install config; do
    [ -f "$FPK/wizard/$w" ] && ok "wizard/$w 存在" || bad "wizard/$w 缺失（install 缺失会导致桌面入口空白页）"
done
if [ -f "$FPK/wizard/install" ]; then
    if grep -q 'wizard_port' "$FPK/wizard/install"; then
        ok "wizard/install 含 wizard_port"
    else
        bad "wizard/install 缺 wizard_port（入口端口会变成字面量 \${wizard_port}）"
    fi
fi

# ui/config 里若用了变量，则 wizard 必须有对应字段
if grep -q '\${wizard_' "$FPK/app/ui/config" 2>/dev/null; then
    ok "ui/config 使用向导变量（前面已确认 wizard/install 有对应字段）"
fi

# 语法检查：按块统计，只有整块无错才打印 ✓
_shell_bad=0
for f in "$FPK"/cmd/*; do
    [ -f "$f" ] || continue
    bash -n "$f" 2>/dev/null || { bad "shell 语法错误: ${f#$FPK/}"; _shell_bad=1; }
done
[ "$_shell_bad" = 0 ] && ok "shell 脚本语法通过"

PY="${PYTHON:-python3}"
# 目录复制前校验受管清单，不能把忽略的本地文件打进包。
if "$PY" - <<'PYEOF'
import json, re, subprocess
from pathlib import Path
root = Path('fpk')
tracked = set(subprocess.check_output(['git', 'ls-files', '-z', '--', 'fpk/']).decode().strip('\0').split('\0'))
actual = set()
for p in root.rglob('*'):
    if '__pycache__' in p.parts or p.suffix == '.pyc' or p.name == '.DS_Store':
        continue
    if p.is_symlink() or not (p.is_file() or p.is_dir()):
        raise SystemExit(f'不支持的打包输入：{p}')
    if p.is_file():
        actual.add(p.as_posix())
if actual != tracked:
    raise SystemExit(f'打包文件与 Git 索引不一致：新增 {sorted(actual-tracked)}，缺少 {sorted(tracked-actual)}')

def read(path):
    return json.loads((root/path).read_text(encoding='utf-8'))

wiz = read('wizard/install')
for name in ('wizard/config', 'config/privilege', 'config/resource'):
    read(name)
manifest = dict(line.split('=', 1) for line in (root/'manifest').read_text(encoding='utf-8').splitlines() if '=' in line)
launcher = read('app/ui/config')['.url'][manifest['desktop_applaunchname']]
fields = {item.get('field') for step in wiz for item in step['items']}
refs = set(re.findall(r'\$\{(wizard_[A-Za-z0-9_]+)\}', json.dumps(launcher)))
if refs - fields:
    raise SystemExit(f'桌面入口使用了未定义的安装向导字段：{sorted(refs-fields)}')
PYEOF
then ok "Git 输入清单和 fnOS JSON 通过"; else bad "Git 输入清单或 fnOS JSON 无效"; fi
if "$PY" - "$FPK/app/server" <<'PYEOF'
import sys
from pathlib import Path
for path in Path(sys.argv[1]).glob('*.py'):
    compile(path.read_bytes(), str(path), 'exec')
PYEOF
then ok "Python 语法通过"; else bad "Python 语法错误"; fi
if command -v node >/dev/null 2>&1; then
    node --check "$FPK/app/www/workspace.js" && ok "JavaScript 语法通过" || bad "JavaScript 语法错误"
else
    warn "未安装 Node.js，跳过单独的 JavaScript 语法检查"
fi

# 无硬编码卷路径（打包进 fpk 的脚本里不该写死 /volN）
if grep -rnE '/vol[0-9]|/usr/local/apps' "$FPK" \
        --include='*.py' --include='*.sh' --include=main \
        --include='*_callback' --include='*_init' 2>/dev/null | grep -q .; then
    bad "发现硬编码卷路径："; grep -rnE '/vol[0-9]|/usr/local/apps' "$FPK" \
        --include='*.py' --include='*.sh' --include=main --include='*_callback' --include='*_init' 2>/dev/null | sed 's/^/      /'
else
    ok "无硬编码卷路径"
fi

if [ "$fail" -gt 0 ]; then
    echo; echo "${RED}打包前检查未通过（$fail 项），终止。${RST}"; exit 1
fi
[ "$CHECK_ONLY" = 1 ] && { echo; echo "${GRN}仅检查模式，未出包。${RST}"; exit 0; }

# ─────────────────────────── 打包 ───────────────────────────
step "打包"

DEST="$ROOT/dist/$VER"
mkdir -p "$ROOT/dist" || exit 1
BUILD=$(mktemp -d "$ROOT/dist/.staging.XXXXXX") || exit 1
trap 'rm -rf -- "$BUILD"' EXIT
[ ! -e "$DEST" ] || { echo "输出已存在，拒绝覆盖：$DEST" >&2; exit 1; }
mkdir "$BUILD/out" || exit 1
OUT="$BUILD/out/jsydiptv-$VER.fpk"

# 生成文件只写入打包副本。
mkdir "$BUILD/src" && cp -a "$FPK/app/"{server,ui,www} "$BUILD/src/" || exit 1
printf '%s' "$VER" > "$BUILD/src/www/VERSION" || exit 1
install -m 644 "$FPK/ICON_256.PNG" "$BUILD/src/www/app-logo.png" || exit 1
install -d -m 755 "$BUILD/src/www/licenses/hls.js" || exit 1
install -m 644 LICENSE "$BUILD/src/www/licenses/jsydiptv-MIT.txt" || exit 1
install -m 644 third-party/hls.js/LICENSE "$BUILD/src/www/licenses/hls.js/LICENSE" || exit 1
install -m 644 third-party/hls.js/README.md "$BUILD/src/www/licenses/hls.js/README.md" || exit 1

# ⚠️ app.tgz 顶层必须直接是 server/ ui/ www/，不能套一层 app/
( cd "$BUILD/src" && tar "${TAR_DET[@]}" --exclude=__pycache__ --exclude='*.pyc' --exclude=.DS_Store -czf "$BUILD/app.tgz" server ui www ) || { echo "${RED}app.tgz 打包失败${RST}"; exit 1; }
ok "app.tgz 已生成"

cp "$FPK"/ICON.PNG "$FPK"/ICON_256.PNG "$FPK"/manifest "$BUILD"/ || { echo "${RED}根文件拷贝失败${RST}"; exit 1; }
cp -r "$FPK"/cmd "$FPK"/config "$FPK"/wizard "$BUILD"/ || { echo "${RED}目录拷贝失败${RST}"; exit 1; }

# 权限：根文件 644、目录与脚本 755
chmod 644 "$BUILD"/ICON.PNG "$BUILD"/ICON_256.PNG "$BUILD"/manifest "$BUILD"/app.tgz
chmod 755 "$BUILD"/cmd "$BUILD"/config "$BUILD"/wizard
chmod 755 "$BUILD"/cmd/* 2>/dev/null
find "$BUILD"/config "$BUILD"/wizard -type f -exec chmod 755 {} + 2>/dev/null

( cd "$BUILD" && tar "${TAR_DET[@]}" -czf "$OUT" ICON.PNG ICON_256.PNG app.tgz manifest cmd config wizard ) \
    || { echo "${RED}fpk 打包失败${RST}"; exit 1; }

# 写构建指纹（供发布核验「这个包是由哪个 commit 构建的」）
printf '{"version":"%s","commit":"%s","dirty_files":%s,"source_date_epoch":%s,"sha256":"%s"}\n' \
    "$VER" "$BUILD_COMMIT" "$BUILD_DIRTY" "$SOURCE_DATE_EPOCH" \
    "$(sha256sum "$OUT" | cut -d' ' -f1)" > "$BUILD/out/build.json"

# ─────────────────────────── B. 打包后 ───────────────────────────
step "B. 打包后验证（包结构层）"

# 根层只有这 4 个文件 + 3 个目录
TOP=$(tar tzf "$OUT" | awk -F/ '{print $1}' | sort -u)
EXPECT=$(printf 'ICON.PNG\nICON_256.PNG\napp.tgz\ncmd\nconfig\nmanifest\nwizard\n')
if [ "$TOP" = "$EXPECT" ]; then ok "根层结构正确（4 文件 + 3 目录）"; else
    bad "根层结构异常："; echo "$TOP" | sed 's/^/      /'; fi

# app.tgz 顶层必须是 server/ui/www
TOPS=$(tar tzf "$BUILD/app.tgz" | awk -F/ '{print $1}' | sort -u | tr '\n' ' ')
if [ "$TOPS" = "server ui www " ]; then ok "app.tgz 顶层 = server/ ui/ www/"; else
    bad "app.tgz 顶层异常：【$TOPS】（套了一层 app/ 会让应用装完缺文件）"; fi

# 逐字节与源码一致
CHK="$BUILD/_verify"; rm -rf "$CHK"; mkdir -p "$CHK/src"
tar xzf "$OUT" -C "$CHK" 2>/dev/null
tar xzf "$CHK/app.tgz" -C "$CHK/src" 2>/dev/null
_diff_bad=0
diff -r -x '__pycache__' -x '*.pyc' -x '.DS_Store' "$CHK/src" "$BUILD/src" >/dev/null 2>&1 \
    || { bad "包内 app.tgz 与构建副本不一致"; _diff_bad=1; }
diff -r "$CHK/cmd" "$FPK/cmd"       >/dev/null 2>&1 || { bad "包内 cmd/ 与源码不一致"; _diff_bad=1; }
diff -r "$CHK/config" "$FPK/config" >/dev/null 2>&1 || { bad "包内 config/ 与源码不一致"; _diff_bad=1; }
for f in ICON.PNG ICON_256.PNG; do
    diff -q "$CHK/$f" "$FPK/$f" >/dev/null 2>&1 || { bad "包内 $f 不一致"; _diff_bad=1; }
done
diff -r "$CHK/wizard" "$FPK/wizard" >/dev/null 2>&1 || { bad "包内 wizard/ 与源码不一致"; _diff_bad=1; }
diff -q "$CHK/manifest" "$FPK/manifest" >/dev/null 2>&1 || { bad "包内 manifest 与源码不一致"; _diff_bad=1; }
[ "$_diff_bad" = 0 ] && ok "逐字节比对通过（包 = 源码）"

# 关键文件都在
# ⚠️ 先把列表缓存成变量再 grep：`tar | grep -q` 在 pipefail 下会因 SIGPIPE 误报失败
APP_LIST=$(tar tzf "$BUILD/app.tgz")
FPK_LIST=$(tar tzf "$OUT")
miss=0
for f in server/server.py server/media_probe.py www/index.html www/workspace.css www/workspace.js www/hls.min.js www/app-logo.png www/VERSION www/licenses/jsydiptv-MIT.txt www/licenses/hls.js/LICENSE www/licenses/hls.js/README.md ui/config ui/images/icon_64.png ui/images/icon_256.png; do
    grep -qx "$f" <<< "$APP_LIST" || { bad "app.tgz 缺 $f"; miss=1; }
done
for f in wizard/install wizard/config manifest ICON.PNG ICON_256.PNG; do
    grep -qx "$f" <<< "$FPK_LIST" || { bad "fpk 缺 $f"; miss=1; }
done
[ "$miss" = 0 ] && ok "关键文件齐全"

# 包里不该出现 __pycache__ / *.pyc / .DS_Store
if grep -qE '__pycache__|\.pyc$|\.DS_Store' <<< "$APP_LIST$FPK_LIST"; then
    bad "包内混入缓存/垃圾文件："; grep -E '__pycache__|\.pyc$|\.DS_Store' <<< "$APP_LIST$FPK_LIST" | sed 's/^/      /'
else
    ok "包内无缓存/垃圾文件"
fi

# 权限抽查
PERM=$(tar tvzf "$OUT" | grep -E ' (manifest|ICON\.PNG)$' | awk '{print $1}' | sort -u | head -1)
case "$PERM" in -rw-r--r--) ok "根文件权限 644" ;; *) bad "根文件权限异常：$PERM" ;; esac
DP=$(tar tvzf "$OUT" | grep -E ' cmd/$' | awk '{print $1}' | head -1)
case "$DP" in drwxr-xr-x) ok "cmd/ 权限 755" ;; *) bad "cmd/ 权限异常：$DP" ;; esac

SIZE=$(stat -c %s "$OUT")
MD5=$(md5sum "$OUT" | cut -d' ' -f1)
SHA256=$(sha256sum "$OUT" | cut -d' ' -f1)

rm -rf "$CHK"
echo
if [ "$fail" -gt 0 ]; then
    echo "${RED}打包后验证失败（$fail 项）—— 该包不可发布。${RST}"; exit 1
fi
[ ! -e "$DEST" ] || { echo "输出已存在，拒绝覆盖：$DEST" >&2; exit 1; }
mv -T "$BUILD/out" "$DEST" || exit 1
echo "${GRN}✓ 出包成功${RST}"
echo "  文件：dist/$VER/jsydiptv-$VER.fpk"
echo "  大小：$SIZE B"
echo "  MD5 ：$MD5"
echo "  SHA256：$SHA256"
echo "  构建于：${BUILD_COMMIT:0:8}$([ "$BUILD_DIRTY" != "0" ] && echo " （⚠️ 工作区有 $BUILD_DIRTY 处未提交改动）")"
echo
echo "${DIM}下一步：按 docs/verification.md 在隔离 fnOS 验收。${RST}"