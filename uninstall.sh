#!/bin/bash
# 卸载 /listen 语音播报：停掉播报进程和手机电台，删掉本工具安装的文件和运行目录。
# 只停运行目录里进程号文件记着、而且命令行核对过确实是本程序 run / phone 的进程；
# 只删内容确实属于本工具的文件（同名的、你自己写的命令文件会保留）。配置文件（里面有你的密钥）默认保留；
# 安装时为你原来的同名文件做的备份也不动，结束时列出来。
# 变量一律写成 ${名字}：macOS 自带的 bash 3.2 会把紧跟在 $名字 后面的中文字节当成变量名的一部分。
set -uo pipefail

[ -n "${HOME:-}" ] || { echo "卸载没有进行：环境变量 HOME 是空的。" >&2; exit 1; }
DEST="${HOME}/.claude/listen"
CMDS="${HOME}/.claude/commands"
RT="${DEST}/run"      # 只认默认位置：不跟随 LISTEN_RT，免得它指向别处时被整个删掉
CONF="${DEST}/config.json"

stopped=""
for f in "${RT}"/*.pid; do
  [ -f "${f}" ] || continue
  case "$(basename "${f}" .pid)" in
    phone) marker="listen.py phone" ;;
    ????????-????-????-????-????????????) marker="listen.py run" ;;
    *) continue ;;
  esac
  pid="$(cat "${f}" 2>/dev/null)"
  case "${pid}" in ''|*[!0-9]*) continue ;; esac
  # 进程号可能已被系统挪给别的程序：命令行里确实是本程序的 run / phone 才停
  if ps -o command= -p "${pid}" 2>/dev/null | grep -qF "${marker}"; then
    kill "${pid}" 2>/dev/null && echo "已停止进程 ${pid}" && stopped="${stopped} ${pid}"
  fi
done
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do     # 等它们收尾（最多约 3 秒），免得退出时又把运行目录建回来
  left=""
  for pid in ${stopped}; do
    kill -0 "${pid}" 2>/dev/null && left=1
  done
  [ -z "${left}" ] && break
  sleep 0.2
done

remove_mine() {   # remove_mine <文件> <本工具文件里特有的一段文字>
  if [ -f "${1}" ] && [ ! -L "${1}" ] && grep -qF "${2}" "${1}"; then
    rm -f "${1}"
    echo "已删除：${1}"
  elif [ -e "${1}" ] || [ -L "${1}" ]; then
    echo "保留：${1} 看起来不是本工具装的文件，没有动它。"
  fi
}
remove_mine "${DEST}/listen.py" "claude-code-listen"
remove_mine "${CMDS}/listen.md" "listen.py toggle"
if [ -e "${RT}" ]; then
  rm -rf "${RT}"
  echo "已删除运行目录：${RT}"
fi

baks=""
for b in "${DEST}"/listen.py.bak-* "${CMDS}"/listen.md.bak-*; do
  [ -e "${b}" ] && baks="${baks}
  ${b}"
done
if [ -n "${baks}" ]; then
  echo "下面这些是安装时为你原来的同名文件做的备份，卸载没有动它们；想恢复就把它改回原名，不需要就手动删除：${baks}"
fi

if [ -e "${CONF}" ]; then
  echo "配置文件保留在：${CONF}（里面有你的密钥）。不再需要的话请手动删除：rm \"${CONF}\""
else
  rmdir "${DEST}" 2>/dev/null || true
fi
