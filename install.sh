#!/bin/bash
# 安装 /listen 语音播报：把 listen.py 和斜杠命令拷到 ~/.claude/ 下；配置文件不存在时才从样例生成（权限 600）。
# 可以放心重复执行（升级也是再跑一遍）：内容相同的文件直接跳过；目标已有不同内容时先备份成
# <名字>.bak-<时间> 再覆盖；已有的配置一律不动；目标是符号链接时拒绝写入，免得顺着链接改到别处的文件。
# 这些检查（含两个目标目录能不能建、能不能写）都在写任何东西之前做完，任何一项不行就整体不动，不会装一半。
# 变量一律写成 ${名字}：macOS 自带的 bash 3.2 会把紧跟在 $名字 后面的中文字节当成变量名的一部分。
set -euo pipefail

fail() {
  echo "安装没有进行：$*" >&2
  exit 1
}

[ -n "${HOME:-}" ] || fail "环境变量 HOME 是空的，不知道该装到哪里。"
[ -d "${HOME}" ] || fail "HOME 指向的 ${HOME} 不是一个目录。"
cd "$(dirname "$0")" || fail "进不了安装脚本所在的目录。"
for f in listen.py commands/listen.md config.example.json; do
  [ -f "${f}" ] || fail "仓库里缺少 ${f}，请在完整的仓库目录里运行。"
done

DEST="${HOME}/.claude/listen"
CMDS="${HOME}/.claude/commands"
CONF="${DEST}/config.json"
STAMP="$(date +%Y%m%d-%H%M%S)"

for t in "${DEST}/listen.py" "${CMDS}/listen.md" "${CONF}"; do
  if [ -L "${t}" ]; then
    fail "${t} 是一个符号链接。为了不顺着链接改到别处的文件，请先自己处理掉它（删掉或换成普通文件）再重新运行。"
  fi
  if [ -e "${t}" ] && [ ! -f "${t}" ]; then
    fail "${t} 已存在，但不是普通文件。"
  fi
  if [ -e "${t}" ] && [ ! -r "${t}" ]; then
    fail "${t} 读不了，没法先备份它。"
  fi
done

for d in "${DEST}" "${CMDS}"; do   # 目录已存在就得能写；不存在就看最近一层已存在的上级能不能写
  e="${d}"
  while [ ! -e "${e}" ] && [ ! -L "${e}" ]; do
    e="$(dirname "${e}")"
  done
  if [ ! -d "${e}" ]; then
    fail "${e} 已存在，但不是目录（要往 ${d} 里装文件）。"
  fi
  [ -w "${e}" ] || fail "没有写 ${e} 的权限（要往 ${d} 里装文件）。"
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "提醒：/listen 目前只支持 macOS（播放要用系统自带的 afplay），在这台电脑上装了也不能出声。"
fi

put() {   # put <源> <目标>：内容相同就跳过；目标已有不同内容就先备份，再经临时文件整个换上
  if [ -e "${2}" ] && cmp -s "${1}" "${2}"; then
    echo "已是最新：${2}"
    return
  fi
  if [ -e "${2}" ]; then
    bak="${2}.bak-${STAMP}-$$"     # 带上进程号（必要时再加序号）：同一秒里装两次也不会盖掉上一份备份
    n=1
    while [ -e "${bak}" ]; do
      bak="${2}.bak-${STAMP}-$$-${n}"
      n=$((n + 1))
    done
    cp -p "${2}" "${bak}"
    echo "已备份原有文件：${bak}"
  fi
  cp "${1}" "${2}.tmp-${STAMP}"
  mv -f "${2}.tmp-${STAMP}" "${2}"
  echo "已安装：${2}"
}

mkdir -p "${DEST}" "${CMDS}"
put listen.py "${DEST}/listen.py"
put commands/listen.md "${CMDS}/listen.md"

if [ -e "${CONF}" ]; then
  echo "配置文件已存在，保留不动：${CONF}"
else
  (umask 077 && cp config.example.json "${CONF}")
  chmod 600 "${CONF}"
  echo "已生成配置文件：${CONF}（权限 600）"
  echo "下一步：打开它，选好 provider 并填上语音服务的密钥，然后运行 /usr/bin/python3 ${DEST}/listen.py doctor 体检。"
fi

if /usr/bin/python3 -c pass 2>/dev/null; then
  /usr/bin/python3 "${DEST}/listen.py" check
else
  echo "提醒：没找到可用的 /usr/bin/python3，请先安装苹果的命令行工具：xcode-select --install"
fi
command -v ffmpeg >/dev/null 2>&1 || [ -x /opt/homebrew/bin/ffmpeg ] || [ -x /usr/local/bin/ffmpeg ] ||
  echo "提醒：没找到 ffmpeg。在这台电脑或远程电脑上出声不受影响；想用手机收听需要先安装（例如 brew install ffmpeg）。"
