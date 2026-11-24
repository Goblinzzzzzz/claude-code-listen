#!/usr/bin/python3
"""claude-code-listen：Claude Code 的 /listen 语音播报。

开启后，本会话里 Claude 每写完一段话，就把它清洗成适合念的文字、切成小块，
调用你配置的语音服务合成 mp3 并播放。只用 macOS 自带的 /usr/bin/python3 和标准库。

由 ~/.claude/commands/listen.md 调用（斜杠命令只做开/关切换，不接受参数）：
  listen.py toggle <会话编号> [on|off]    开/关这个会话的播报；不带 on/off 就是切换
  listen.py run <会话编号> <Claude进程号>  播报进程本体（toggle 自动拉起，不用手动跑）
  listen.py phone                        手机电台（toggle 自动拉起）：给手机播放器听的不间断音频流
  listen.py status                       哪些会话开着播报、手机电台在不在、收听地址
  listen.py doctor                       逐项体检配置与依赖，最后试合成一句（不出声）
  listen.py token --new                  更换收听令牌：旧地址立刻失效，正在收听的设备被断开
  listen.py say <一句话>                  单独试音
  listen.py check                        自检（不联网、不出声）

配置文件默认是 ~/.claude/listen/config.json（环境变量 LISTEN_CONFIG 可改），
运行时文件（进程号、日志、临时音频）放 ~/.claude/listen/run/（LISTEN_RT 可改）。
密钥只用来发请求：不打印、不写日志，任何输出里都只说"已填写/未填写"。

为什么盯记录文件而不用钩子：钩子触发的那一刻这段话还没落盘（实测晚约 0.1~0.2 秒）；
而且钩子要写进 settings.json，有的配置切换工具每次启动都会整份重写那个文件，钩子会被抹掉。
"""
import base64
import codecs
import errno
import fcntl
import glob
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

VERSION = "1.0.0"

# ---- 可调的几个数 ----
CHUNK = 120     # 每次合成的字数上限：合成耗时随字数增长，块太大开口就慢
FIRST = 40      # 每段话的第一块切得更短，开口才快
KEEP = 2        # 说话赶不上干活时，只留最近这么多段，旧的步骤播报丢掉
IDLE = 7200     # Claude 进程不在了以后，记录文件再这么多秒没动静才退出（见 run 里的说明）
NUDGE = 30      # "没地方出声"的终端提示，最多这么多秒提醒一次
SYNTH_DEADLINE = 60     # 一次合成从发出到收完的总期限（墙钟）：慢慢滴数据的服务也拖不过它
MAX_LINE = 5 << 20      # 记录里单行超过 5MB 就跳过：要念的话不会这么长，硬解析只会拖垮进程
LOG_MAX = 1 << 20       # 日志超过 1MB 就把旧的改名为 listen.log.1（只留一份）
HEADER_DEADLINE = 5     # 电台：从连上算起，这么多秒内没读完请求头就断开（按墙钟算，慢慢发也拖不过）
PENDING_PER_IP = 2      # 电台：同一来源地址最多同时有这么多个还没通过令牌校验的连接
RADIO_GONE = 2          # 交给电台排队的音频没人播了（电台进程不在了）时，"播放器"报的退出码

HOME = os.path.expanduser("~")
RT = os.environ.get("LISTEN_RT") or HOME + "/.claude/listen/run"
CONFIG = os.environ.get("LISTEN_CONFIG") or HOME + "/.claude/listen/config.json"
SPOOL = RT + "/phone-spool"     # 等着送往手机的音频排在这里
ERR_SOUND = "/System/Library/Sounds/Basso.aiff"
# ffmpeg 只给手机电台用（生成静音和提示音）。从 Claude Code 里起的进程 PATH 未必全，常见位置也找一下。
FFMPEG = shutil.which("ffmpeg") or next(
    (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.access(p, os.X_OK)), None)
# 校验一律用 fullmatch（不写 ^$）：$ 会放过结尾的换行
SID_RE = re.compile(r"[0-9a-f-]{36}")
DEVICE_RE = re.compile(r"[a-z0-9-]+")
# ssh 别名会原样进命令行：只认这些字符，也不许以 - 开头冒充 ssh 的参数
ALIAS_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
KEY_RE = re.compile(r"[\x21-\x7e]+")        # 密钥一类的值：可见的英文字符，不含空格换行
TOKEN_RE = re.compile(r"[0-9a-f]{32}")      # 收听令牌：128 位随机量
TAG = uuid.uuid4().hex[:12]     # 本进程推到远程电脑的临时音频都带这个前缀，打断时只清自己的
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ControlMaster=auto",
       "-o", "ControlPath=~/.ssh/cm-listen-%C", "-o", "ControlPersist=300"]   # 复用连接，第二句起不用再握手
NAMES = {"here": "这台电脑的扬声器", "phone": "手机", "": "手机（但播放器还没在收听）"}
FIELDS = {"openai": ("base_url", "api_key", "model", "voice"),
          "volcengine": ("endpoint", "api_key", "app_id", "access_key", "resource_id", "voice")}
# 火山引擎常见错误码 → 程序自己写的说明。服务端返回的 message 原文一律不显示：里面可能有打了码的密钥或回显的文本
VOLC_CODES = {40402003: "文本超出长度上限", 45000000: "音色没有权限（常见于 resource_id 与音色不匹配）、请求格式不对或超出并发",
              45000001: "请求参数无效", 45002000: "音色无效", 45002001: "没有可朗读的文本", 55000000: "服务端内部错误"}
SECRETS = []    # 本进程读到过的密钥，只用来从要输出的文字里抹掉它们
_conf = None


class ConfigError(Exception):
    """配置有问题。消息是给人看的中文提示：去改哪个文件的哪一项。"""


class SpeechError(RuntimeError):
    """本程序自己写的失败说明（合成、播放），可以原样给人看。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """一律不跟随跳转：urllib 默认会把密钥请求头原样带到跳转后的另一台主机。"""

    def redirect_request(self, *args):
        return None


_net = threading.local()    # 当前线程正在发的那个请求：看门狗到点要能找到它的连接掐断


def _track(cls):
    """包一层连接类：建连接时把它记进当前线程的 _net.holder，好让合成的总期限一到就掐断它。"""
    def make(*args, **kwargs):
        conn = cls(*args, **kwargs)
        holder = getattr(_net, "holder", None)
        if holder is not None:
            real = conn.connect

            def connect():
                real()
                # 记下这条 socket：收到响应头后连接对象会放掉对它的引用，读正文时只能从这里掐断
                holder["sock"] = conn.sock
            conn.connect = connect
        return conn
    return make


class _HTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_track(http.client.HTTPConnection), req)


class _HTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        extra = {"check_hostname": self._check_hostname} if hasattr(self, "_check_hostname") else {}
        return self.do_open(_track(http.client.HTTPSConnection), req, context=self._context, **extra)


OPENER = urllib.request.build_opener(_NoRedirect, _HTTP, _HTTPS)   # 本程序所有 HTTP 请求都走它


def tilde(path):
    """家目录下的路径显示成 ~/…：输出里不带出系统用户名。"""
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


def shell_path(path):
    """给人照抄进终端的路径：~/ 留在引号外，好让 shell 展开。"""
    t = tilde(path)
    return "~/" + shlex.quote(t[2:]) if t.startswith("~/") else shlex.quote(t)


def keep_secret(value):
    """记下要从输出里抹掉的密钥。短于 8 位的不记：那样会把正常文字搅乱，还能从被抹的位置反推出密钥。"""
    if isinstance(value, str) and len(value.strip()) >= 8:
        SECRETS.extend([value, value.strip()])


def scrub(text):
    """把读到过的密钥从文字里抹掉。正常路径上密钥根本不进任何消息，这里是最后一道保险。"""
    for k in sorted(set(SECRETS), key=len, reverse=True):
        text = text.replace(k, "***")
    return text


def rt_dir(sub=""):
    """建运行目录（及其子目录），一律 700：里面有待播的音频、收听令牌和日志。"""
    os.makedirs(RT, mode=0o700, exist_ok=True)
    if sub:
        os.makedirs(RT + sub, mode=0o700, exist_ok=True)
    return RT + sub


def log(msg):
    """写一行日志。超过 LOG_MAX 就把旧的改名为 .1（只留一份），日志不会无限长。
    日志写不进去（比如文件句柄用完了）时不让程序跟着崩：这时也没有别处可报。"""
    try:
        rt_dir()
        path = RT + "/listen.log"
        try:
            if os.path.getsize(path) > LOG_MAX:
                os.replace(path, path + ".1")
        except FileNotFoundError:
            pass
        with open(path, "a") as f:
            f.write("%s [%d] %s\n" % (time.strftime("%m-%d %H:%M:%S"), os.getpid(), scrub(msg)))
    except OSError:
        pass


def where_failed(ex):
    """异常 → "类型（listen.py 第 N 行 函数名）"。只说出错的位置，不带异常消息：消息里可能有记录内容。"""
    tb = traceback.extract_tb(ex.__traceback__)
    tb = [f for f in tb if os.path.basename(f.filename) == os.path.basename(__file__)] or tb   # 优先报本文件里的位置
    if not tb:
        return type(ex).__name__
    return "%s（%s 第 %d 行 %s）" % (type(ex).__name__, os.path.basename(tb[-1].filename), tb[-1].lineno, tb[-1].name)


def describe(ex):
    """异常 → 给人看（也给日志）的一句中文。本程序自己写的说明（ConfigError、SpeechError）原样用；
    别的只带异常类型（文件读写错误再带上缩写成 ~ 的文件名），不带 Python 的英文原话、异常栈和绝对路径。"""
    if isinstance(ex, (ConfigError, SpeechError)):
        return scrub(str(ex))
    if isinstance(ex, OSError) and ex.filename:
        return "读写 %s 时出错（%s）" % (tilde(str(ex.filename)), type(ex).__name__)
    return "出了意料之外的错（%s）" % type(ex).__name__


def quiet_crashes():
    """后台进程（播报、电台）里任何没接住的异常只记一行类型与位置，不往日志里写异常栈。"""
    sys.excepthook = lambda kind, value, tb: log("未处理的错误：%s" % where_failed(value))
    threading.excepthook = lambda a: log("线程里未处理的错误：%s" % where_failed(a.exc_value))


def read_config():
    """读出配置文件原样的内容，只确认它是个 JSON 对象；顺手记下里面的密钥，供 scrub 抹除。"""
    shown = tilde(CONFIG)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c = json.load(f)
    except FileNotFoundError:
        raise ConfigError("找不到配置文件 %s：请先运行 install.sh，或照 config.example.json 建一份，"
                          "再填上语音服务的密钥。" % shown) from None
    except UnicodeDecodeError:
        raise ConfigError("配置文件 %s 不是 UTF-8 编码的文本。" % shown) from None
    except json.JSONDecodeError as e:
        raise ConfigError("配置文件 %s 不是合法的 JSON（第 %d 行第 %d 列附近），请检查逗号、引号和括号。"
                          % (shown, e.lineno, e.colno)) from None
    except OSError as e:
        raise ConfigError("读不了配置文件 %s：%s" % (shown, e.strerror)) from None
    if not isinstance(c, dict):
        raise ConfigError("配置文件 %s 的最外层应该是 { … } 对象。" % shown)
    for p in FIELDS:
        s = c.get(p)
        for k in ("api_key", "access_key"):
            if isinstance(s, dict):
                keep_secret(s.get(k))
    env, p = os.environ.get("LISTEN_API_KEY", "").strip(), c.get("provider")
    if env:     # 环境变量里的密钥优先于配置文件
        keep_secret(env)
        if isinstance(p, str) and isinstance(c.get(p), dict):
            c[p]["api_key"] = env
    return c


def check_config(c):
    """逐项核对配置并补上默认值；有问题就抛 ConfigError，只说哪一项不对，从不带出填的值。"""
    def bad(what):
        raise ConfigError("%s 里的 %s" % (tilde(CONFIG), what))

    def number(v, low):
        return not isinstance(v, bool) and isinstance(v, (int, float)) and v >= low

    p = c.get("provider")
    if not isinstance(p, str) or p not in FIELDS:
        bad("provider 只能填 openai 或 volcengine。")
    s = c.get(p)
    if not isinstance(s, dict):
        bad("\"%s\": { … } 这一节缺了，provider 选了它就要填它。" % p)
    for k in FIELDS[p]:
        if not isinstance(s.setdefault(k, ""), str):
            bad("%s.%s 应该是字符串（用双引号括起来）。" % (p, k))
        s[k] = s[k].strip()
    for k in FIELDS[p]:
        if not s[k] and k not in ("api_key", "app_id", "access_key"):
            bad("%s.%s 还没填。" % (p, k))
    if p == "openai" and not s["api_key"]:
        bad("openai.api_key 还没填（也可以用环境变量 LISTEN_API_KEY 提供）。")
    if p == "volcengine" and not s["api_key"] and not (s["app_id"] and s["access_key"]):
        bad("火山引擎的鉴权还没填：新版控制台填 volcengine.api_key（也可以用环境变量 LISTEN_API_KEY 提供），"
            "旧版控制台填 volcengine.app_id 和 volcengine.access_key。")
    for k in ("api_key", "app_id", "access_key"):
        if s.get(k) and not KEY_RE.fullmatch(s[k]):
            bad("%s.%s 里有空格、换行或中文之类的字符，请确认是否完整复制。" % (p, k))
    url = s["base_url" if p == "openai" else "endpoint"]
    if not re.fullmatch(r"https?://[^\s\x00-\x1f\x7f]+", url):
        bad("%s.%s 应该以 http:// 或 https:// 开头，中间不能有空格或换行。" % (p, "base_url" if p == "openai" else "endpoint"))
    if not number(s.setdefault("speed", 1.0), 0) or not s["speed"]:
        bad("%s.speed 应该是大于 0 的数字，1.0 是原速。" % p)
    eh = s.setdefault("extra_headers", {})
    if not isinstance(eh, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                           and not re.search(r"[\r\n]", k + v) for k, v in eh.items()):
        bad("%s.extra_headers 应该写成 {\"请求头名\": \"值\"}，值里不能有换行。" % p)
    if not number(c.setdefault("volume", 1.0), 0):
        bad("volume 应该是不小于 0 的数字，1.0 是原音量。")
    if not isinstance(c.setdefault("switch_cue", "另一个对话："), str):
        bad("switch_cue 应该是一句话（字符串），留空表示不念。")
    ph = c.setdefault("phone", {})
    if not isinstance(ph, dict):
        bad("phone 应该是 { … } 对象。")
    port = ph.setdefault("port", 18945)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        bad("phone.port 应该是 1~65535 之间的整数。")
    if not isinstance(ph.setdefault("advertise_host", ""), str):
        bad("phone.advertise_host 应该是字符串，留空表示自动探测。")
    most = ph.setdefault("max_listeners", 8)
    if isinstance(most, bool) or not isinstance(most, int) or most < 1:
        bad("phone.max_listeners 应该是不小于 1 的整数（同时在线的连接数上限）。")
    for name in ("devices", "remotes"):
        d = c.setdefault(name, {})
        if not isinstance(d, dict):
            bad("%s 应该是 { … } 对象。" % name)
        for k, v in d.items():
            if name == "devices" and not DEVICE_RE.fullmatch(k):
                bad("devices 里的设备名「%s」不合法：只能用小写字母、数字和 -。" % k.strip())
            if not isinstance(v, str) or not ALIAS_RE.fullmatch(v):
                bad("%s 里「%s」对应的 ssh 别名不合法：只能用字母、数字和 . _ -，且不能以 - 开头。" % (name, k.strip()))
    return c


def conf():
    """读取并核对配置。每个进程只读一次：改了配置要重新 /listen 才生效。"""
    global _conf
    if _conf is None:
        _conf = check_config(read_config())
    return _conf


def perm_note():
    """配置文件里有密钥：权限比 600 宽就提醒一句（不阻止）。"""
    try:
        mode = os.stat(CONFIG).st_mode & 0o777
    except OSError:
        return ""
    if not mode & 0o077:
        return ""
    return ("配置文件 %s 的权限是 %03o，这台电脑上的其他账户也能读到里面的密钥，建议执行：chmod 600 %s"
            % (tilde(CONFIG), mode, shell_path(CONFIG)))


def plain_http(url):
    """http:// 而且不是本机回环地址：密钥和要念的文字会不加密地经过网络。"""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http":
        return False
    host = (u.hostname or "").lower()
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def notes(c):
    """配置里不阻止使用、但值得提醒的事，每条一句：密钥过短、明文地址。"""
    p, s = c["provider"], c[c["provider"]]
    out = ["%s.%s 短于 8 位，密钥过短，疑似填错。" % (p, k) for k in ("api_key", "access_key")
           if s.get(k) and len(s[k]) < 8]
    field = "base_url" if p == "openai" else "endpoint"
    if plain_http(s[field]):
        out.append("%s.%s 是 http:// 明文地址：密钥和回复内容会不加密地经过网络，建议改用 https://。" % (p, field))
    return out


def clean(md):
    """把屏幕上的写法变成适合念的：代码块、表格不念，符号去掉。"""
    md = re.sub(r"```.*?(```|\Z)", "\n", md, flags=re.S)
    md = re.sub(r"^\s*(\|.*|[-=*_]{3,})\s*$", "", md, flags=re.M)        # 表格行、分隔线
    md = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", md)                    # 链接只念文字
    md = re.sub(r"https?://\S+", "链接", md)
    md = re.sub(r"^\s*(#{1,6}|[-*+>]|\d+[.、)])\s+", "", md, flags=re.M)  # 标题/列表/引用的记号
    return re.sub(r"[*`~#]+", "", md)


def chunks(text):
    """按句子（和逗号）切，再拼成不超过 CHUNK 字的小块；没有实际文字的块丢掉。"""
    out, cur = [], ""
    for s in re.findall(r"[^。！？!?；;，\n]+[。！？!?；;，]?", text):
        s = s.strip()
        if not re.search(r"\w", s):
            continue
        if cur and len(cur) + len(s) > (CHUNK if out else FIRST):
            out.append(cur)
            cur = ""
        if cur and cur[-1] not in "。！？!?；;，,：:":
            cur += "，"     # 原文靠换行断开的两行，念的时候要有停顿
        cur += s
    return out + [cur] if cur else out


def narration(block):
    """后台会话里，工具调用前说的话有时被记成带 narration 标记的 thinking 块；真正的思考不念。

    ponytail: 靠签名开头的标记来认，是未公开的格式。哪天认不出了，表现是"步骤播报变少"
    （最终回复不受影响），到时改这里。
    """
    sig, text = block.get("signature"), block.get("thinking")
    if block.get("type") != "thinking" or not isinstance(text, str) or not text or not isinstance(sig, str):
        return ""
    try:
        head = base64.b64decode(sig[:64])
    except ValueError:
        return ""
    return text if b"narration" in head else ""


def speech_rate(speed):
    """配置里的倍速 → 火山引擎的 speech_rate：0 是原速，100 是 2 倍速，-50 是 0.5 倍速。"""
    return max(-50, min(100, round((speed - 1) * 100)))


def speech_url(base):
    """兼容 OpenAI 的语音接口地址；base_url 已经以 /v1 结尾就不再重复加。"""
    base = base.rstrip("/")
    return base + ("/audio/speech" if base.endswith("/v1") else "/v1/audio/speech")


def volc_error(code):
    """火山引擎的错误码 → 程序自己写的一句说明。服务端给的 message 原文不用。"""
    if isinstance(code, bool) or not isinstance(code, int):
        return "火山引擎返回错误（错误码不是数字）"
    return "火山引擎返回错误 code=%d（%s）" % (code, VOLC_CODES.get(code, "未知错误码"))


def http_error(e, volc):
    """HTTP 错误只报状态码；火山引擎再带上数字错误码和程序自己写的说明。
    服务端返回的文字一律不带：有的服务会在错误正文里回显打了码的密钥或请求的文本。"""
    if 300 <= e.code < 400:
        return "语音服务返回了跳转（状态码 %d），已拒绝跟随：请检查地址是否填对" % e.code
    msg = "语音服务返回 HTTP %d" % e.code
    if volc:
        try:
            j = json.loads(e.read(65536).decode("utf-8"))
            if isinstance(j, dict) and "code" in j:
                msg += "，" + volc_error(j["code"])
        except (OSError, ValueError, RecursionError, http.client.HTTPException):
            pass
    return msg


def net_why(e):
    """网络错误 → 程序自己写的说明。不带对方发来的任何文字（乱码的状态行、证书里的名字等）。"""
    r = getattr(e, "reason", None) or e
    if isinstance(r, ssl.SSLError):
        tag = r.reason if isinstance(r.reason, str) and re.fullmatch(r"[A-Z0-9_]+", r.reason) else ""
        return "加密连接（TLS）失败" + ("（%s）" % tag if tag else "")
    if isinstance(r, socket.gaierror):
        return "域名解析失败"
    if isinstance(r, http.client.RemoteDisconnected):
        return "对方没回任何响应就断开了连接"
    if isinstance(r, http.client.HTTPException):
        return "对方回的不是正常的 HTTP 响应（%s）" % type(r).__name__
    if isinstance(r, OSError) and r.errno:
        return {errno.ECONNREFUSED: "对方拒绝连接（地址或端口不对，或服务没开）", errno.ECONNRESET: "连接被对方断开",
                errno.ETIMEDOUT: "连接超时", errno.EHOSTUNREACH: "到不了对方主机",
                errno.ENETUNREACH: "网络不通"}.get(r.errno, "网络错误（%s）" % errno.errorcode.get(r.errno, r.errno))
    return "请求没发出去（%s）" % type(r).__name__


def _watchdog(holder, done, secs):
    """合成的总期限：到点还没完就掐断连接（每次收数据的 30 秒超时管不住"慢慢滴"的服务）。
    连接还没建好时隔半秒再试，直到请求结束。"""
    if done.wait(secs):
        return
    holder["cut"] = True
    while not done.is_set():
        sock = holder.get("sock")
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        done.wait(0.5)


def post(url, headers, body, read, volc=False):
    """发请求并用 read 读出音频。每次收数据最多等 30 秒，整个请求最多 SYNTH_DEADLINE 秒（到点掐断，
    合成线程不会被占死）；连接类错误自动重试一次（合成不改动任何东西，重发是安全的）。
    不跟随任何跳转（见 OPENER）；错误消息全是程序自己写的，不带服务端返回的文字。"""
    data = json.dumps(body, ensure_ascii=False).encode()
    for attempt in (1, 2):
        holder, done = {}, threading.Event()
        _net.holder = holder
        threading.Thread(target=_watchdog, args=(holder, done, SYNTH_DEADLINE), daemon=True).start()
        try:
            with OPENER.open(urllib.request.Request(url, data=data, headers=headers), timeout=30) as r:
                got = read(r)
            if holder.get("cut"):       # 掐断之后可能只读到半截
                raise SpeechError("语音服务 %d 秒内没有完成合成，已放弃" % SYNTH_DEADLINE)
            return got
        except Exception as e:
            if holder.get("cut"):
                raise SpeechError("语音服务 %d 秒内没有完成合成，已放弃" % SYNTH_DEADLINE) from None
            if isinstance(e, SpeechError):
                raise
            if isinstance(e, urllib.error.HTTPError):
                raise SpeechError(http_error(e, volc)) from None
            if isinstance(e, socket.timeout) or isinstance(getattr(e, "reason", None), socket.timeout):
                raise SpeechError("语音服务 30 秒没有回应") from None   # 超时不重试：再等，这段话早过时了
            if not isinstance(e, (urllib.error.URLError, ConnectionError, http.client.HTTPException)):
                raise
            if attempt == 2:
                raise SpeechError("连不上语音服务：%s" % net_why(e)) from None
            log("连接语音服务出错，重试一次：%s" % net_why(e))
        finally:
            done.set()
            _net.holder = None


def volc_audio(blocks):
    """火山引擎的响应（一块块的字节）→ 拼好的 mp3 字节。

    响应是分块传来的一串 JSON 对象：可能一行一个，也可能首尾相接，一个对象还可能被拆在两个网络块里，
    所以不按换行切，而是在缓冲区上用 raw_decode 一个个往下解。每个对象的 data 是一段 base64 音频，
    按顺序拼起来；code 0 是正常片段，20000000 是结束，其它都是错误。
    """
    try:
        return _volc_audio(blocks)
    except (ValueError, TypeError, RecursionError):     # 坏 base64、data 不是字符串、嵌套过深、不是 UTF-8……
        raise SpeechError("火山引擎返回了看不懂的内容") from None


def _volc_audio(blocks):
    dec, text, buf, out = json.JSONDecoder(), codecs.getincrementaldecoder("utf-8")(), "", []
    for block in blocks:
        buf += text.decode(block)
        while True:
            buf = buf.lstrip()
            try:
                obj, end = dec.raw_decode(buf)
            except ValueError:
                break       # 缓冲区空了，或者这个对象还没收全：等下一块
            buf = buf[end:]
            if not isinstance(obj, dict):
                raise SpeechError("火山引擎返回了看不懂的内容")
            if obj.get("data"):
                out.append(base64.b64decode(obj["data"]))
            if obj.get("code") == 20000000:
                return b"".join(out)
            if obj.get("code") != 0:
                raise SpeechError(volc_error(obj.get("code")))
    if (buf + text.decode(b"", final=True)).strip():
        raise SpeechError("火山引擎返回了看不懂的内容")
    raise SpeechError("火山引擎的响应提前结束了（没收到结束标记 code=20000000）")


def tts(text):
    """一小块文字 → mp3 字节。失败一律抛错（消息里不带密钥），绝不返回空音频。"""
    c = conf()
    p, s = c["provider"], c[c["provider"]]
    h = {"User-Agent": "claude-code-listen/" + VERSION, "Content-Type": "application/json"}
    if p == "openai":
        h["Authorization"] = "Bearer " + s["api_key"]
        h.update(s["extra_headers"])
        body = {"model": s["model"], "voice": s["voice"], "input": text, "response_format": "mp3",
                "speed": s["speed"]}
        raw = post(speech_url(s["base_url"]), h, body, lambda r: r.read())
    else:
        if s["api_key"]:        # 新版控制台：一个 API Key 就够
            h["X-Api-Key"] = s["api_key"]
        else:                   # 旧版控制台：APP ID + Access Token
            h["X-Api-App-Id"], h["X-Api-Access-Key"] = s["app_id"], s["access_key"]
        h["X-Api-Resource-Id"], h["X-Api-Request-Id"] = s["resource_id"], str(uuid.uuid4())
        h.update(s["extra_headers"])
        body = {"req_params": {"text": text, "speaker": s["voice"], "audio_params": {
            "format": "mp3", "sample_rate": 24000, "speech_rate": speech_rate(s["speed"])}}}
        raw = post(s["endpoint"], h, body, lambda r: volc_audio(iter(lambda: r.read(8192), b"")), volc=True)
    # 有的服务对无效音色会回 200 加空正文，只看状态码会"哑了还报成功"
    if not is_mp3(raw):
        raise SpeechError("语音服务返回的不是 mp3 音频（%d 字节）" % len(raw))
    return raw


def synth(text, outdir):
    """一小块文字 → 一个 mp3 文件。失败一律抛错，不返回空文件。"""
    raw = tts(text)
    fd, path = tempfile.mkstemp(suffix=".mp3", dir=outdir)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path


def volume():
    """0~1，系统音量之上的再一道；环境变量 LISTEN_VOLUME 优先（测试时设 0 保持安静）。"""
    return str(float(os.environ.get("LISTEN_VOLUME") or conf()["volume"]))


def play(path, where="here"):
    """在 where 那里放这个音频文件：'here' 是本机，'phone' 是手机电台，其余是 ssh 别名。"""
    if where == "here":
        return subprocess.Popen(["afplay", "-v", volume(), path])
    if where == "phone":
        return Spooled(path)
    # 文件内容走标准输入送过去，对方落成临时文件再放。临时目录用 mktemp 随机生成、umask 077 只有本人能读
    # （防同机别的账户偷听或预先放好同名链接）；名字带本进程的前缀 TAG，打断时只清自己的。退出码原样带回来。
    # 整段用 sh -c 包起来：对方账户的登录 shell 是 fish、tcsh 之类也照样成立。
    ext = ".aiff" if path == ERR_SOUND else ".mp3"
    remote = ("umask 077; d=$(mktemp -d /tmp/listen-tts-%s-XXXXXX) || exit 1; f=\"$d/a%s\"; "
              "cat > \"$f\"; afplay -v %s \"$f\"; s=$?; rm -rf \"$d\"; exit $s") % (TAG, ext, volume())
    with open(path, "rb") as f:
        return subprocess.Popen(SSH + [where, "sh -c " + shlex.quote(remote)], stdin=f,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


BITRATES = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
            2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def frames(data):
    """mp3 字节 → [(一帧的字节, 这一帧多少秒)]。跳过开头的标签和认不出的字节。

    电台要把多段语音和静音接成一条流，必须整帧整帧地接，不能在帧中间切换。
    """
    out, i, n = [], 0, len(data)
    if data[:3] == b"ID3" and n > 10:
        i = 10 + ((data[6] & 127) << 21 | (data[7] & 127) << 14 | (data[8] & 127) << 7 | (data[9] & 127))
    while i + 4 <= n:
        b1, b2 = data[i + 1], data[i + 2]
        ver, layer, br, sr = (b1 >> 3) & 3, (b1 >> 1) & 3, b2 >> 4, (b2 >> 2) & 3
        if data[i] != 0xFF or b1 & 0xE0 != 0xE0 or ver == 1 or layer != 1 or br in (0, 15) or sr == 3:
            i += 1
            continue
        rate, v1 = RATES[ver][sr], ver == 3
        size = (144 if v1 else 72) * BITRATES[1 if v1 else 2][br] * 1000 // rate + ((b2 >> 1) & 1)
        if i + size > n:
            break
        out.append((data[i:i + size], (1152 if v1 else 576) / rate))
        i += size
    return out


def frame_format(frame):
    """一帧的帧头 → (采样率, 声道数)。声道模式 3 是单声道，其余（立体声、联合立体声、双声道）都算 2。"""
    return RATES[(frame[1] >> 3) & 3][(frame[2] >> 2) & 3], 1 if frame[3] >> 6 == 3 else 2


def is_mp3(raw):
    """开头是 ID3 标签或 MPEG 帧同步字节，并且真能解析出音频帧。"""
    head = raw[:3] == b"ID3" or (len(raw) > 1 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0)
    return head and bool(frames(raw))


def made(kind, fmt, secs):
    """用 ffmpeg 生成一段指定格式（采样率, 声道）的静音或 440Hz 提示音，存进运行目录，之后一直用缓存。"""
    rate, ch = fmt
    path = "%s/%s-%d-%d.mp3" % (RT, kind, rate, ch)
    if not os.path.exists(path):
        src = "anullsrc=r=%d" % rate if kind == "silence" else "sine=frequency=440:sample_rate=%d" % rate
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", src, "-t", str(secs), "-ar", str(rate),
                        "-ac", str(ch), "-b:a", "64k", "-write_xing", "0", "-id3v2_version", "0",
                        path + ".tmp.mp3"], check=True)
        os.replace(path + ".tmp.mp3", path)
    with open(path, "rb") as f:
        return f.read()


def token(new=False):
    """收听地址里的随机令牌（128 位），防同一网络里别人随手就能听。生成一次后固定，`token --new` 更换。

    文件缺失、为空、长度或字符不对都重新生成，绝不会出现"空令牌 → /.mp3 就能听"。新令牌先写进临时文件
    再放到位；首次生成用 link（目标已存在就失败），两个进程同时首次生成时只有一个算数，另一个改用它的。
    """
    path = RT + "/token"
    if not new:
        t = saved_token()
        if t and TOKEN_RE.fullmatch(t):
            return t
    rt_dir()
    t, tmp = secrets.token_hex(16), "%s.%s.tmp" % (path, uuid.uuid4().hex)
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(t)
    try:
        if new or os.path.lexists(path):
            os.replace(tmp, path)       # 换新令牌，或者旧文件坏了
            return t
        try:
            os.link(tmp, path)
        except FileExistsError:
            return token()              # 别的进程刚抢先生成了：用它的
        return t
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def saved_token():
    """令牌文件里现在写的是什么；文件不存在返回 None。其它读错误（权限、句柄用完）照实抛出：
    读不了不等于没有，不能因此换掉令牌，否则正在收听的设备会全被断开。"""
    try:
        with open(RT + "/token") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def host():
    """收听地址里给人看的主机：配置里写了就用它；否则先试 Tailscale，再取本机默认网卡的地址。"""
    named = conf()["phone"]["advertise_host"]
    if named:
        return named
    for cmd in (["tailscale", "ip", "-4"], ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"]):
        try:
            ip = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.split()
        except (OSError, subprocess.SubprocessError):
            continue
        if ip and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip[0]):
            return ip[0]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))     # UDP 的 connect 不发任何数据包，只是让系统选出走默认路由的网卡
            ip = s.getsockname()[0]
        if not ip.startswith(("0.", "127.")):
            return ip
    except OSError:
        pass
    return "<这台电脑的地址>"


def phone_url():
    return "http://%s:%d/%s.mp3" % (host(), conf()["phone"]["port"], token())


def ps(pid, field):
    return subprocess.run(["ps", "-o", field + "=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def pid_of(path, marker):
    """进程号文件里记的进程还活着、而且真是我们的进程才返回它：进程号可能已被系统挪给别的程序。"""
    try:
        with open(path) as f:
            pid = int(f.read())
    except (OSError, ValueError):
        return 0
    return pid if pid > 0 and marker in ps(pid, "command") else 0


def phone_pid():
    return pid_of(RT + "/phone.pid", "listen.py phone")


def live_sessions():
    """开着播报的会话：进程号文件对应的进程确实活着、而且是播报进程。陈旧的进程号文件顺手清掉
    （留 10 秒余量：toggle 刚建好、子进程还没跑起来的那一刻不能误删）。"""
    on = []
    for f in sorted(glob.glob(RT + "/*.pid")):
        sid = os.path.basename(f)[:-4]
        if not SID_RE.fullmatch(sid):
            continue
        if pid_of(f, "listen.py run"):
            on.append(sid)
            continue
        try:
            if time.time() - os.path.getmtime(f) > 10:
                os.remove(f)
        except OSError:
            pass
    return on


def phone_listeners():
    """现在有几台设备在收听电台。电台进程不在就算 0，免得把音频交给一个没人管的队列。"""
    if not phone_pid():
        return 0
    try:
        with open(RT + "/phone-listeners") as f:
            return int(f.read() or 0)
    except (OSError, ValueError):
        return 0


class Slots:
    """手机电台的连接名额：同时在线的连接不超过 most 个；同一来源地址同时最多 PENDING_PER_IP 个
    还没通过令牌校验的连接（一个地址不停地开空连接，也占不满别的地址的名额）。"""

    def __init__(self, most):
        self.most, self.active, self.pending, self.ok, self.lock = most, 0, {}, set(), threading.Lock()

    def take(self, ip):
        with self.lock:
            if self.active >= self.most or self.pending.get(ip, 0) >= PENDING_PER_IP:
                return False
            self.active += 1
            self.pending[ip] = self.pending.get(ip, 0) + 1
            return True

    def verified(self, ip, conn):
        """这条连接通过了令牌校验：不再算这个地址的"未验证连接"。"""
        with self.lock:
            self.ok.add(conn)
            self._unpend(ip)

    def release(self, ip, conn):
        with self.lock:
            self.active -= 1
            if conn in self.ok:
                self.ok.discard(conn)
            else:
                self._unpend(ip)

    def _unpend(self, ip):
        self.pending[ip] -= 1
        if not self.pending[ip]:
            del self.pending[ip]


class Spooled:
    """手机这条路上的"播放器"：把音频交给电台排队，电台播完会删掉这个文件。"""

    def __init__(self, path):
        rt_dir("/phone-spool")
        base = "%s/%d-%d" % (SPOOL, time.time_ns(), os.getpid())
        if path == ERR_SOUND:       # 提示音要和流里正在用的格式一致，由电台自己生成，这里只放个记号
            self.path = base + ".beep"
            open(self.path, "w").close()
        else:
            self.path = base + ".mp3"
            shutil.copyfile(path, self.path + ".tmp")
            os.replace(self.path + ".tmp", self.path)
        self.checked = time.time()

    def poll(self):
        """念完了返回 0；电台进程不在了返回 RADIO_GONE（这段永远不会有人删，不能一直等着占着排队锁）。"""
        if not os.path.exists(self.path):
            return 0
        if time.time() - self.checked >= 1:     # 每秒看一眼电台还在不在
            self.checked = time.time()
            try:
                gone = not phone_pid()
            except OSError:                     # 一时查不了（比如句柄紧张）：当作还在，下一秒再看
                gone = False
            if gone:
                _rm(self.path)
                return RADIO_GONE
        return None

    def wait(self):
        while True:
            code = self.poll()
            if code is not None:
                return code
            time.sleep(0.1)

    def kill(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
        with open(RT + "/phone-flush", "w") as f:    # 告诉电台：正在念的这段别念了
            f.write(str(time.time_ns()))


def phone():
    """手机电台：一条永不结束的 mp3 流，有话说时送语音，没话说时送静音。

    做成"电台"而不是一段段发，是因为手机上的播放器只有在持续播放时才不会被系统在后台掐掉。
    一条流里前后的采样率和声道必须一致，否则播放器会放成怪声；不同语音服务给的格式不同，
    所以静音和提示音按 (采样率, 声道) 各备一套，开播时按 24k 单声道，之后跟随最近一段语音的格式。
    起不来时把原因写进运行目录的 phone-error，toggle 据此如实告诉用户手机收听不可用。
    """
    import queue
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    quiet_crashes()
    rt_dir("/phone-spool")
    os.chmod(RT, 0o700)
    token()         # 开播前确保有一个合格的令牌（缺了、空了、不对就重新生成）
    err = RT + "/phone-error"
    try:
        os.remove(err)
    except OSError:
        pass

    def fail(why):
        log("手机电台：没开成 —— " + why)
        try:
            with open(err, "w") as f:
                f.write(why)
        except OSError:
            pass

    if not FFMPEG:
        return fail("没找到 ffmpeg（例如 brew install ffmpeg）")
    port, most = conf()["phone"]["port"], conf()["phone"]["max_listeners"]
    sounds = {}

    def sound(fmt):
        """(采样率, 声道) → (一秒静音的帧, 提示音的帧)，每种格式只生成一次。"""
        if fmt not in sounds:
            sounds[fmt] = (frames(made("silence", fmt, 1)), frames(made("beep", fmt, 0.3)))
        return sounds[fmt]

    fmt = (24000, 1)
    try:
        quiet, beep = sound(fmt)
    except (OSError, subprocess.CalledProcessError):
        return fail("ffmpeg 生成静音失败")
    listeners, lock, slots, warned_req = set(), threading.Lock(), Slots(most), [0]

    def publish():
        try:
            with open(RT + "/phone-listeners", "w") as f:
                f.write(str(len(listeners)))
        except OSError:     # 资源一时紧张时写不进去：听众数下一次再报，电台照常
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64

        def process_request(self, request, client_address):
            """同时在线的连接到上限、或这个来源地址已有 PENDING_PER_IP 个还没通过令牌校验的连接时，
            直接关掉新连接、不为它起线程：一批空闲连接耗不光线程和文件句柄，一个地址也占不满名额。"""
            if not slots.take(client_address[0]):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except (RuntimeError, OSError):     # 起不了新线程：退回名额、关掉这个连接，电台本身照常
                slots.release(client_address[0], request)
                self.shutdown_request(request)

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                slots.release(client_address[0], request)

        def handle_error(self, request, client_address):
            """请求处理中的任何异常（比如对方发完就复位）：只记一行来源与类型，十秒最多一次；不写异常栈。"""
            if time.time() - warned_req[0] > 10:
                warned_req[0] = time.time()
                log("手机电台：处理来自 %s 的请求时出错（%s）" % (client_address[0], type(sys.exc_info()[1]).__name__))

    class Handler(BaseHTTPRequestHandler):
        timeout = HEADER_DEADLINE   # 每次收数据最多等这么久
        server_version, sys_version = "claude-code-listen", ""     # 响应头里不暴露 Python 版本

        def setup(self):
            super().setup()
            # 读完请求头的总期限：从连上算起 HEADER_DEADLINE 秒（按墙钟）。光靠每次收数据的超时，
            # 每隔几秒滴一个字节的连接永远不会断，名额就一直被占着
            self.deadline = threading.Timer(HEADER_DEADLINE, self.expire)
            self.deadline.daemon = True
            self.deadline.start()

        def expire(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self.deadline.cancel()
            super().finish()

        def log_message(self, *args):
            pass

        def do_GET(self):
            mine = token()      # 每次都现读：令牌换了马上生效
            if self.path.split("?")[0] != "/%s.mp3" % mine:
                self.close_connection = True    # 路径不对：立刻回 404 并断开，不留着占名额
                self.send_error(404)
                return
            self.deadline.cancel()
            slots.verified(self.client_address[0], self.request)
            self.connection.settimeout(30)      # 开始送音频后放宽：手机偶尔卡一下不至于被断开
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            q = queue.Queue(400)
            for frame, _ in quiet:          # 先补一秒静音（当前格式），让播放器尽快起播
                q.put(frame)
            with lock:
                listeners.add(q)
                publish()
            log("手机电台：有设备开始收听 %s" % self.client_address[0])
            try:
                k = 0
                while True:
                    self.wfile.write(q.get(timeout=10))
                    k += 1
                    if k % 40 == 0:     # 令牌换了：旧地址上的连接一秒内断开（读不了就当没换，接着播）
                        try:
                            if saved_token() != mine:
                                break
                        except OSError:
                            pass
            except (OSError, queue.Empty):
                pass
            finally:
                with lock:
                    listeners.discard(q)
                    publish()
                log("手机电台：设备断开 %s" % self.client_address[0])

    try:
        server = Server(("0.0.0.0", port), Handler)
    except OSError as e:
        return fail("端口 %d 已被别的程序占用" % port if e.errno == errno.EADDRINUSE else
                    "端口 %d 用不了（%s）" % (port, os.strerror(e.errno) if e.errno else type(e).__name__))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with open(RT + "/phone.pid", "w") as f:
        f.write(str(os.getpid()))
    publish()
    log("手机电台：开播 端口=%d" % port)

    def flush_mark():
        try:
            return os.path.getmtime(RT + "/phone-flush")
        except OSError:
            return 0

    speech, cur, clock, n, busy, flushed, warned = [], None, time.monotonic(), 0, time.time(), flush_mark(), 0
    try:
        while True:
            n += 1
            try:
                if n % 5 == 0 or not speech:
                    if flush_mark() != flushed:             # 被打断：正在念的这段剩下的不念了
                        flushed, speech = flush_mark(), []
                    if not speech:
                        if cur:
                            try:
                                os.remove(cur)              # 播完就删，交音频的那一方靠这个知道"念完了"
                            except OSError:
                                pass
                            cur = None
                        for name in sorted(x for x in os.listdir(SPOOL) if x.endswith((".mp3", ".beep"))):
                            cur = SPOOL + "/" + name
                            if not listeners:               # 没人听就不积压
                                speech = []
                            elif name.endswith(".beep"):
                                speech = list(beep)
                            else:
                                try:
                                    with open(cur, "rb") as f:
                                        speech = frames(f.read())
                                except OSError:
                                    speech = []
                                if speech and frame_format(speech[0][0]) != fmt:   # 换了格式：静音、提示音也跟着换
                                    quiet, beep = sound(frame_format(speech[0][0]))
                                    fmt = frame_format(speech[0][0])
                            if speech:
                                break
                            try:
                                os.remove(cur)
                            except OSError:
                                pass
                            cur = None
                    if n % 50 == 0:                         # 没有会话开着播报、也没人在听，十分钟后收摊
                        if live_sessions() or listeners:
                            busy = time.time()
                        elif time.time() - busy > float(os.environ.get("LISTEN_PHONE_IDLE", "600")):
                            break
            except (OSError, subprocess.CalledProcessError) as e:
                # 文件句柄等资源一时紧张、或新格式的静音没生成出来：这一轮照常送静音，下一轮再看，电台不崩
                if time.time() - warned > 10:
                    warned = time.time()
                    log("手机电台：这一轮没处理完（%s），先送静音" % where_failed(e))
            frame, secs = speech.pop(0) if speech else quiet[n % len(quiet)]
            with lock:
                for q in list(listeners):
                    try:
                        q.put_nowait(frame)
                    except queue.Full:                  # 这台设备收得太慢：放弃它，别拖住别人
                        listeners.discard(q)
                        publish()
            clock += secs
            ahead = clock - time.monotonic() - 0.2      # 按真实时间的节奏发，只领先 0.2 秒，打断才来得及
            if ahead > 0:
                time.sleep(ahead)
            elif ahead < -1:
                clock = time.monotonic()                # 落后太多（比如机器睡过一觉）就重新对表
    except Exception as ex:     # 任何意料之外的退出：先把原因写进日志，不让电台悄无声息地没了
        log("手机电台意外退出：%s" % where_failed(ex))
    finally:
        for name in ("phone.pid", "phone-listeners"):
            try:
                os.remove(RT + "/" + name)
            except OSError:
                pass
        log("手机电台：收摊")


def halt(player, where):
    """立刻停。远程那头不会因为这边断开就停，得再补一刀：只清本进程（TAG）、本账户的临时音频和播放进程。"""
    player.kill()
    if where not in ("here", "phone"):
        cmd = "rm -rf /tmp/listen-tts-%s-*; pkill -U \"$(id -u)\" -f /tmp/listen-tts-%s-" % (TAG, TAG)
        subprocess.Popen(SSH + [where, "sh -c " + shlex.quote(cmd)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def pick(clients, who, own_tty="", named=None, devices=None, remotes=None):
    """用户此刻在哪台设备上对话 → 'here' / ssh 别名 / ''（收不了声音的设备，按手机算）。

    clients 是 tmux 各客户端的「最近活动时刻 终端名」，取最近在打字的那个；
    没有 tmux 时看 Claude 自己所在的终端。再到 who 的输出里查这个终端是从哪个地址连进来的。
    远程设备先按它自报的名字认（named：终端 → 设备名，见 README），认不出再按来源地址认。
    """
    rows = [l.split() for l in clients.splitlines() if len(l.split()) == 2 and l.split()[0].isdigit()]
    tty = max(rows, key=lambda r: int(r[0]))[1] if rows else own_tty
    tty = tty.replace("/dev/", "")
    for line in who.splitlines():
        f = line.split()
        if len(f) >= 2 and f[1] == tty:
            m = re.search(r"\(([^)]+)\)\s*$", line)
            if not m:
                return "here"
            return (devices or {}).get((named or {}).get(tty, "")) or (remotes or {}).get(m.group(1), "")
    return "here"


def target(cpid=0):
    """这一刻该在哪出声：'here' / ssh 别名 / 'phone' / ''（没地方出声）。"""
    forced = os.environ.get("LISTEN_TARGET")    # 手动指定/测试用：here、phone、ssh 别名，或空串表示不出声
    if forced is not None:
        if forced not in ("here", "phone", "") and not ALIAS_RE.fullmatch(forced):
            raise ConfigError("环境变量 LISTEN_TARGET 只能是 here、phone、空，或一个合法的 ssh 别名。")
        return forced

    def out(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    named = {}
    for f in glob.glob(RT + "/tty/*"):
        try:
            with open(f) as fh:
                named[os.path.basename(f)] = fh.read().strip()
        except OSError:
            pass
    c = conf()
    where = pick(out(["tmux", "list-clients", "-F", "#{client_activity} #{client_tty}"]),
                 out(["who"]), ps(cpid, "tty") if cpid else "", named, c["devices"], c["remotes"])
    # 不认识的远程设备按手机算：手机播放器正在收听就送过去，没在收听就没地方出声
    return "phone" if where == "" and phone_listeners() else where


def where_name(where):
    return NAMES.get(where) or "远程电脑 %s 的扬声器" % where


def speaker(sid):
    """记下"现在轮到谁说话"，并回答：上一个说话的是不是别的会话。"""
    path = RT + "/speaker"
    try:
        with open(path) as f:
            prev = f.read().strip()
    except OSError:
        prev = ""
    if prev != sid:
        with open(path, "w") as f:
            f.write(sid)
    return bool(prev) and prev != sid


def cue_file():
    """换会话时念的那句提示（配置项 switch_cue），合成一次后按语音服务、音色和语速缓存。"""
    c = conf()
    s = c[c["provider"]]
    key = json.dumps([c["provider"], s["voice"], s["speed"], c["switch_cue"]], ensure_ascii=False)
    path = "%s/cue-%s.mp3" % (RT, hashlib.sha1(key.encode()).hexdigest()[:12])
    if not os.path.exists(path):
        os.replace(synth(c["switch_cue"], RT), path)
    return path


def nudge(text):
    """在每个连着的 tmux 客户端底部闪一行字（几秒后自己消失）。没有 tmux 就算了。"""
    try:
        ttys = subprocess.run(["tmux", "list-clients", "-F", "#{client_tty}"],
                              capture_output=True, text=True, timeout=5).stdout.split()
        for tty in ttys:
            subprocess.run(["tmux", "display-message", "-c", tty, "-d", "6000", text], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def focus(sid=None):
    """记录用户最近在哪个会话发过言（只用来在 status 里显示；出声靠排队，每个会话都会念）。"""
    path = RT + "/focus"
    if sid:
        with open(path + ".tmp", "w") as f:
            f.write(sid)
        os.replace(path + ".tmp", path)
        return sid
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def claude_pid():
    """顺着父进程往上找到所属的 Claude 进程（找不到返回 0）。"""
    pid = os.getppid()
    for _ in range(6):
        if os.path.basename(ps(pid, "comm")).startswith("claude"):
            return pid
        parent = ps(pid, "ppid")
        if not parent.isdigit() or int(parent) <= 1:
            break
        pid = int(parent)
    return 0


def entry(e):
    """一行记录 → ("user", None) 表示用户在这个会话发言，("assistant", [要念的文字…])，或 None（跟播报无关）。

    记录格式没有公开，什么形状都可能出现：类型不对的一律当作没有，不抛错。
    """
    if not isinstance(e, dict) or e.get("isSidechain") or e.get("isMeta"):
        return None
    m = e.get("message")
    content = m.get("content") if isinstance(m, dict) else None
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    if e.get("type") == "user":
        return None if any(b.get("type") == "tool_result" for b in blocks) else ("user", None)
    if e.get("type") == "assistant":
        texts = [b.get("text") if b.get("type") == "text" else narration(b) for b in blocks]
        return "assistant", [t for t in texts if isinstance(t, str) and t]
    return None


def discard(futs):
    """丢掉还没念的合成：没开始的直接取消；已经合成好或正在合成的，等它完成后把生成的临时音频删掉。"""
    for fut in futs:
        if not fut.cancel():
            fut.add_done_callback(_drop_file)


def _drop_file(fut):
    try:
        os.remove(fut.result())
    except Exception:   # 这次合成本来就失败了（没有文件），或者文件已经删掉：都不用管
        pass


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def play_failed(where, code):
    """播放进程非零退出 → 给人看的一句话，说清是哪台设备。"""
    if where == "phone":
        return "手机电台不在了（可能崩溃或被关掉），这段没放出来"
    if where == "here":
        return "这台电脑上播放失败（afplay 退出码 %d），这段没放出来" % code
    if code == 255:
        return "连不上远程电脑 %s（ssh 失败），这段没放出来" % where
    return "远程电脑 %s 上播放失败（退出码 %d），这段没放出来" % (where, code)


def start_radio():
    """拉起手机电台（输出不接日志：电台自己只用 log() 记一行一行的话，不写异常栈）。"""
    return subprocess.Popen([sys.executable, os.path.abspath(__file__), "phone"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def run(sid, cpid):
    quiet_crashes()
    rt_dir()
    c = conf()
    idle = float(os.environ.get("LISTEN_IDLE") or IDLE)
    tmp = tempfile.mkdtemp(prefix="tts-", dir=RT)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    pool = ThreadPoolExecutor(4)        # 边念边合成后面的；留余量给"已发出又被丢弃"的请求
    pattern = HOME + "/.claude/projects/*/%s.jsonl" % sid
    path = next(iter(glob.glob(pattern)), None)
    pos = os.path.getsize(path) if path else 0      # 只念开启之后的新内容
    queue, player, playing, msg, last, where, nudged = [], None, None, 0, time.time(), "here", [0]
    lock, held, why = open(RT + "/play.lock", "a"), False, "收到停止信号"
    log("开始播报 会话=%s Claude进程=%s 语音服务=%s" % (sid, cpid, c["provider"]))

    def notice(text):
        """在 tmux 底部提示一句，30 秒内最多一次。"""
        if time.time() - nudged[0] > NUDGE:
            nudged[0] = time.time()
            nudge(text)

    try:
        # 不能"Claude 进程一没就退出"：同一个会话被重新拉起（比如 claude --resume）时进程号会变，
        # 那样用户以为还开着、其实没人念了。所以进程不在以后，只要记录文件还在更新就继续干。
        while (cpid and alive(cpid)) or time.time() - last < idle:
            time.sleep(0.2)
            path = path or next(iter(glob.glob(pattern)), None)
            if not path:
                continue
            with open(path, "rb") as f:
                f.seek(pos)
                data = f.read()
            lines = data.split(b"\n")
            pos += len(data) - len(lines.pop())     # 没写完的半行留到下一轮
            for line in lines:
                if not line.strip():
                    continue
                last = time.time()
                if len(line) > MAX_LINE:
                    log("跳过一行过长的记录（超过 %d MB）" % (MAX_LINE >> 20))
                    continue
                try:            # 逐行保护：解析和处理里的任何错误（含嵌套过深、内存不够）只跳过这一行
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    got = entry(e)
                    if got and got[0] == "user":
                        # 用户在这个会话开口了：像聊天一样，这个会话没念完的旧内容不再念；别的会话不受影响
                        focus(sid)
                        discard(f for _, f in queue)
                        queue = []
                        if player:
                            halt(player, where)
                            player = None
                            if playing:
                                _rm(playing)
                                playing = None
                    elif got and got[1] and not target(cpid):
                        # 没地方出声：别静悄悄丢掉，在终端底部提示一下
                        notice("语音播报：手机上的播放器没在收听，这段话没念出来（在播放器里打开收听地址即可）"
                               if FFMPEG else "语音播报：手机收听需要先安装 ffmpeg，这段话没念出来")
                    elif got and got[1]:
                        for t in got[1]:
                            parts = chunks(clean(t))
                            if parts:
                                msg += 1
                                queue += [(msg, pool.submit(synth, p, tmp)) for p in parts]
                        discard(f for m, f in queue if m <= msg - KEEP)
                        queue = [(m, f) for m, f in queue if m > msg - KEEP]
                except Exception as ex:     # 单行处理出错：只记类型和位置（不带记录正文），跳过这一行
                    log("跳过一行处理不了的记录：%s" % where_failed(ex))
            # 几个会话都开着播报时排队念：谁拿到这把锁谁念，念完手头这一批就放开，别的会话等着，不会叠音。
            if held and not player and not queue:
                fcntl.flock(lock, fcntl.LOCK_UN)
                held = False
            code = player.poll() if player else None     # 只问一次：交给电台的"播放器"再问就会丢掉结果
            if code is not None:
                player = None
                if playing:
                    _rm(playing)
                    playing = None
                if code:            # 播放失败（ssh 连不上、afplay 出错、电台没了）：记日志并提示，不静悄悄
                    log("播放失败：" + play_failed(where, code))
                    notice("语音播报：" + play_failed(where, code))
                    if where == "phone" and FFMPEG and not phone_pid():
                        start_radio()       # 电台中途没了：重新拉起，手机重连就能接着听
                        log("手机电台不在了，已重新拉起")
            if not player and queue and queue[0][1].done() and not held:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                except OSError:
                    continue        # 别的会话正在念，下一轮再看
                where = target(cpid)
                if speaker(sid) and c["switch_cue"] and where:  # 上一个说话的是别的会话：先念一句提示
                    try:
                        player, playing = play(cue_file(), where), None
                        continue
                    except Exception as ex:     # 提示语可有可无：没念成就记一笔，正文照念
                        log("换会话的提示语没念成：%s" % describe(ex))
            if not player and queue and queue[0][1].done():
                fut = queue.pop(0)[1]
                try:
                    playing = fut.result()
                    where = target(cpid)    # 每一块开播前再看一眼用户在哪台设备上
                    if where:
                        player = play(playing, where)
                    else:
                        _rm(playing)
                        playing = None
                except Exception as ex:     # 合成失败：记日志 + 响一声提示音，这一段剩下的不念了
                    log("合成失败：%s" % describe(ex))
                    discard(f for _, f in queue)
                    queue, playing = [], None
                    where = target(cpid)
                    player = play(ERR_SOUND, where) if where else None
        why = "Claude 进程已不在，记录文件 %d 秒没有更新" % idle
    except Exception as ex:     # 任何意料之外的退出：先把原因（类型与位置，不含记录正文）写进日志
        why = "意外退出"
        log("播报进程意外退出：%s" % where_failed(ex))
    finally:
        if player:
            halt(player, where)
        shutil.rmtree(tmp, ignore_errors=True)
        pidfile = RT + "/%s.pid" % sid
        try:
            with open(pidfile) as f:
                mine = f.read().strip() == str(os.getpid())
            if mine:
                os.remove(pidfile)
        except OSError:
            pass
        log("停止播报 会话=%s（%s）" % (sid, why))
        os._exit(0)     # 不等还在路上的合成请求


def radio_wait(proc, since):
    """等刚拉起的手机电台真的在监听（最多约 2 秒）。起来了返回空串，否则返回起不来的原因。"""
    for _ in range(25):
        if phone_pid():         # 电台绑好端口之后才写进程号文件
            return ""
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    try:
        if os.path.getmtime(RT + "/phone-error") >= since - 1:
            with open(RT + "/phone-error") as f:
                return f.read().strip() or "电台没起来"
    except OSError:
        pass
    return "电台 2 秒内没有起来（详见日志 %s）" % tilde(RT + "/listen.log")


def toggle(sid, want=""):
    if not SID_RE.fullmatch(sid):
        return "语音播报：没拿到会话编号，开关没动。"
    want = want.strip().lower()
    if want not in ("", "on", "off"):
        return "语音播报：只认 on 或 off（不带参数就是开/关切换），开关没动。"
    if sys.platform != "darwin":
        return "语音播报：目前只支持 macOS（播放要用系统自带的 afplay），这台电脑上开不了。"
    rt_dir()
    os.chmod(RT, 0o700)     # 运行目录里有待播的音频和收听令牌，只给自己看
    pidfile = RT + "/%s.pid" % sid
    old = pid_of(pidfile, "listen.py run")
    want_on = {"on": True, "off": False}.get(want, not old)
    if old and not want_on:
        os.kill(old, signal.SIGTERM)
        os.remove(pidfile)
        return "语音播报：已关闭。"
    if want_on and not old:
        c = conf()      # 配置有问题就在这里报出来，不要开了却不出声
        cpid = claude_pid()
        where = target(cpid)
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", sid, str(cpid)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        with open(pidfile, "w") as f:
            f.write(str(p.pid))
        focus(sid)
        radio = "" if FFMPEG else "没找到 ffmpeg（例如 brew install ffmpeg）"
        if FFMPEG and not phone_pid():  # 手机电台顺手拉起来；没人听时它只是空转，十分钟没会话就自己退出
            since = time.time()
            radio = radio_wait(start_radio(), since)
        if not where and radio:
            msg = ("语音播报：已开启，但手机收听不可用：%s。\n现在的出声位置是手机，所以暂时听不到；"
                   "回到这台电脑或登记过的远程电脑上对话时照常出声，这两处不受影响。" % radio)
        else:
            msg = "语音播报：已开启。出声位置：%s" % where_name(where)
            if not where:
                msg += "\n请先在手机播放器（如 VLC）里打开下面这个地址，再回来对话：\n%s" % phone_url()
            elif radio:
                msg += "\n提醒：手机收听不可用：%s；这台电脑和远程电脑出声不受影响。" % radio
        return msg + "".join("\n提醒：" + x for x in [perm_note()] + notes(c) if x)
    return "语音播报：本来就是%s状态，没有变化。" % ("开启" if old else "关闭")


def status():
    on = live_sessions()
    print("开着播报的会话：%s" % ("%d 个" % len(on) if on else "没有"))
    for s in on:
        print("  " + s)
    print("最近发言的会话：%s" % (focus() or "无"))
    why = ""
    if not phone_pid():
        try:
            with open(RT + "/phone-error") as f:
                why = "（上次没开成：%s）" % f.read().strip()
        except OSError:
            pass
    print("手机电台：%s%s" % ("在运行" if phone_pid() else "没在运行", why))
    print("正在收听的设备：%d 台" % phone_listeners())
    try:
        print("收听地址：%s" % phone_url())
    except (ConfigError, OSError) as e:     # 配置不对，或令牌文件读不了（被换成目录、没权限）
        print("收听地址：暂时给不出 —— %s" % describe(e))
    if not FFMPEG:
        print("提醒：没找到 ffmpeg，手机电台开不了（例如 brew install ffmpeg）")


def doctor():
    """逐项体检，最后试合成一句（不播放）。返回有没有"问题"级别的项。输出里的路径都把家目录写成 ~。"""
    bad = []

    def item(level, name, text):
        print("[%s] %s：%s" % (level, name, text))
        if level == "问题":
            bad.append(name)

    print("claude-code-listen %s 体检" % VERSION)
    if sys.platform != "darwin":
        item("问题", "系统", "目前只支持 macOS（播放要用系统自带的 afplay）")
    if os.path.exists(CONFIG):
        item("正常", "配置文件", tilde(CONFIG))
        perm = perm_note()
        item("提醒" if perm else "正常", "配置文件权限", perm or "只有你自己能读")
    else:
        item("问题", "配置文件", "%s 不存在 —— 请先运行 install.sh，或照 config.example.json 建一份" % tilde(CONFIG))
    try:
        raw = read_config()
        p = raw.get("provider")
        if isinstance(p, str) and p in FIELDS:
            item("正常", "语音服务（provider）", p)
            s = raw.get(p) if isinstance(raw.get(p), dict) else {}
            env = os.environ.get("LISTEN_API_KEY", "").strip()
            for k in FIELDS[p]:
                filled = isinstance(s.get(k), str) and bool(s[k].strip())
                print("       %s.%s：%s%s" % (p, k, "已填写" if filled else "未填写",
                                             "（取自环境变量 LISTEN_API_KEY）" if k == "api_key" and env else ""))
        c = conf()
        item("正常", "配置检查", "通过")
        for x in notes(c):
            item("提醒", "配置提醒", x)
    except ConfigError as e:
        item("问题", "配置检查", str(e))
    for name, path, level, missing in (
            ("ffmpeg", FFMPEG, "提醒", "没找到 —— 手机收听需要先安装（例如 brew install ffmpeg）；这台电脑和远程电脑出声不受影响"),
            ("afplay", shutil.which("afplay"), "问题", "没找到（macOS 自带）"),
            ("tmux", shutil.which("tmux"), "提醒", "没找到 —— 不影响使用，出声位置改按 Claude 所在的终端判断")):
        item("正常" if path else level, name, tilde(path) if path else missing)
    if "配置检查" in bad:
        item("提醒", "试合成", "跳过（先把配置填好）")
        return not bad
    try:
        fr = frames(tts("你好，这是一次语音试合成。"))
        rate, ch = frame_format(fr[0][0])
        item("正常", "试合成", "成功，收到约 %.1f 秒音频（%dHz，%s）"
             % (sum(s for _, s in fr), rate, "单声道" if ch == 1 else "立体声"))
    except Exception as ex:     # 体检的最后一道：任何失败都如实报出来，不让它变成异常栈
        item("问题", "试合成", "失败 —— %s" % describe(ex))
    return not bad


def say(text):
    d = tempfile.mkdtemp()
    try:
        where = target(claude_pid())
        print("出声位置：%s" % where_name(where))
        for c in chunks(clean(text)) if where else []:
            code = play(synth(c, d), where).wait()
            if code:        # 播放失败不能当成试音成功
                raise SpeechError(play_failed(where, code))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check():
    """自检：不联网、不出声、不碰真实的运行目录和配置。每组单独报 通过/不通过，全过才算自检通过。"""
    import io
    groups = []

    def group(fn):
        groups.append(fn)
        return fn

    sig = lambda tag: base64.b64encode(b"\x08\x04\x12\x00" + tag + b"\x12" * 60).decode()

    @group
    def 文本清洗与分句():
        assert clean("## 结论\n- **改好了** `a.py`\n```py\nx=1\n```\n| a | b |\n见 [文档](http://x.y)") \
            .split() == ["结论", "改好了", "a.py", "见", "文档"]
        assert chunks("第一句。第二句！\n\n。\n第三行没标点\n第四行") == ["第一句。第二句！第三行没标点，第四行"]
        assert chunks("甲" * 30 + "，" + "乙" * 30 + "。") == ["甲" * 30 + "，", "乙" * 30 + "。"]
        long = chunks("这是一句话。" * 50)
        assert len(long) > 1 and len(long[0]) <= FIRST and all(len(c) <= CHUNK for c in long)

    @group
    def 旁白识别():
        assert narration({"type": "thinking", "thinking": "我去查一下", "signature": sig(b"narration")}) == "我去查一下"
        assert narration({"type": "thinking", "thinking": "内心想法", "signature": sig(b"thinking")}) == ""
        assert narration({"type": "thinking", "thinking": "", "signature": "x"}) == ""
        # 记录格式变了也不能出错：签名是 null、思考内容不是字符串
        assert narration({"type": "thinking", "thinking": "x", "signature": None}) == ""
        assert narration({"type": "thinking", "thinking": ["x"], "signature": sig(b"narration")}) == ""

    @group
    def 出声位置判定():
        # ttys000 从远程电脑连进来，ttys006 从手机连进来，ttys005 是本机终端
        who = "u console 10 22 01:06 \nu ttys000 11 22 22:25 (100.64.0.10)\nu ttys005 11 17 23:16 \n" \
              "u ttys006 11 23 06:28 (192.0.2.10)\n"
        dev, rem = {"laptop": "laptop-ssh"}, {"100.64.0.10": "laptop-ssh"}
        assert pick("100 /dev/ttys000\n200 /dev/ttys006\n", who, "", None, dev, rem) == ""    # 最近在手机上打字
        assert pick("300 /dev/ttys000\n200 /dev/ttys006\n", who, "", None, dev, rem) == "laptop-ssh"
        assert pick("300 /dev/ttys005\n", who, "", None, dev, rem) == "here"
        assert pick("", who, "ttys006", None, dev, rem) == "" and pick("", who, "??") == "here"   # 没有 tmux
        assert pick("9 /dev/ttys006\n", who, "", {"ttys006": "laptop"}, dev, rem) == "laptop-ssh"  # 自报身份
        assert pick("9 /dev/ttys006\n", who, "", {"ttys006": ""}, dev, rem) == ""          # 没自报、地址也不认识
        assert pick("9 /dev/ttys005\n", who, "", {"ttys005": "laptop"}, dev, rem) == "here"  # 本机终端不信残留记录
        assert pick("300 /dev/ttys000\n", who) == ""        # 没登记任何设备：远程连进来的一律按手机

    @group
    def mp3帧解析与格式():
        # 24k 单声道 64k 一帧 192 字节 24 毫秒；44.1k 立体声 128k 一帧 417 字节
        fr = b"\xff\xf3\x84\xc4" + b"\0" * 188
        got = frames(b"ID3\x04\0\0\0\0\0\x05tagxx" + fr + b"junk" + fr + fr[:100])
        assert [len(f) for f, _ in got] == [192, 192] and abs(got[0][1] - 0.024) < 1e-9
        assert frame_format(got[0][0]) == (24000, 1)
        fr44 = b"\xff\xfb\x90\x40" + b"\0" * 413
        got = frames(fr44 * 2)
        assert len(got) == 2 and frame_format(got[0][0]) == (44100, 2) and abs(got[0][1] - 1152 / 44100) < 1e-9
        assert is_mp3(fr * 3) and is_mp3(b"ID3\x04\0\0\0\0\0\0" + fr) and not is_mp3(b"") \
            and not is_mp3(b"<html>") and not is_mp3(b"ID3\x04\0\0\0\0\0\0")

    @group
    def 火山引擎响应解析():
        # 首尾相接、夹换行、被拆在几个网络块里（连一个汉字都被拆开）、错误 code、没有结束标记
        def piece(code, data=b"", message=""):
            return json.dumps({"code": code, "message": message,
                               "data": base64.b64encode(data).decode() if data else None}, ensure_ascii=False)
        whole = (piece(0, b"ab", "正常") + piece(0, b"cd") + "\n" + piece(20000000, message="ok")).encode()
        assert volc_audio([whole]) == b"abcd"
        cut = whole.index("正".encode()) + 1
        assert volc_audio([whole[:5], whole[5:cut], whole[cut:cut + 9], whole[cut + 9:]]) == b"abcd"
        for bad_reply, words in (((piece(0, b"ab") + piece(45000001, message="x")).encode(), "45000001"),
                                 (piece(0, b"ab").encode(), "结束标记"), (b"<html>oops</html>", "看不懂")):
            try:
                volc_audio([bad_reply])
                raise AssertionError("火山引擎的错误响应没有报错")
            except RuntimeError as ex:
                assert words in str(ex), ex
        # 服务端返回的文字一律不进输出：只留数字 code 和程序自己写的中文说明
        leak = "SERVERMSG key=SECR****2345 text=回复正文"
        for reply, code, words in (((piece(0, b"ab") + piece(45000001, message=leak)).encode(), "45000001",
                                    "请求参数无效"), (piece(12345, message=leak).encode(), "12345", "未知错误码")):
            try:
                volc_audio([reply])
                raise AssertionError("火山引擎的错误响应没有报错")
            except RuntimeError as ex:
                assert code in str(ex) and words in str(ex) and "SERVERMSG" not in str(ex) \
                    and "回复正文" not in str(ex), ex
        body = json.dumps({"code": 45000000, "message": leak}).encode()
        msg = http_error(urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, io.BytesIO(body)), True)
        assert "401" in msg and "45000000" in msg and "SERVERMSG" not in msg and "SECR" not in msg, msg
        msg = http_error(urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, io.BytesIO(body)), False)
        assert "401" in msg and "45000000" not in msg and "SERVERMSG" not in msg, msg
        # 解不开的内容（坏 base64、data 不是字符串、嵌套过深、不是 UTF-8）一律说"看不懂"，不出 Python 的英文原话
        deep = "[" * 5000 + "]" * 5000
        for reply in (b'{"code":0,"data":"abc"}', b'{"code":0,"data":5}', ('{"code":0,"data":%s}' % deep).encode(),
                      b'{"code":0,"message":"\xff"}'):
            try:
                volc_audio([reply])
                raise AssertionError("解不开的火山引擎响应没有报错")
            except Exception as ex:
                assert str(ex) == "火山引擎返回了看不懂的内容", "%s: %s" % (type(ex).__name__, str(ex)[:80])
        deep_body = io.BytesIO(('{"code":%s}' % deep).encode())
        msg = http_error(urllib.error.HTTPError("https://x", 500, "x", {}, deep_body), True)
        assert msg == "语音服务返回 HTTP 500", msg
        msg = net_why(OSError(errno.EPIPE, "Broken pipe"))
        assert "Broken" not in msg and "EPIPE" in msg, msg

    @group
    def 倍速换算与接口地址():
        assert [speech_rate(x) for x in (1, 1.1, 0.5, 2, 3, 0.2, 1.234)] == [0, 10, -50, 100, 100, -50, 23]
        assert speech_url("https://api.example.com/") == speech_url("https://api.example.com/v1/") \
            == "https://api.example.com/v1/audio/speech"

    @group
    def 配置核对():
        # ssh 别名会进命令行，设备名要和 ~/.zshenv 里记下的一致
        def cfg(**top):
            c = {"provider": "openai", "openai": {"base_url": "https://api.example.com", "api_key": "k-123456",
                                                  "model": "m", "voice": "v"}}
            c.update(top)
            return c

        def rejected(c):
            try:
                check_config(c)
                return ""
            except ConfigError as ex:
                return str(ex)
        assert not rejected(cfg(devices={"my-laptop-2": "laptop.local"}, remotes={"192.0.2.10": "lab_mac-1"}))
        for c in (cfg(devices={"laptop": "-oProxyCommand"}), cfg(devices={"laptop": "a b"}),
                  cfg(devices={"laptop": "x;rm"}), cfg(devices={"Laptop": "ok"}), cfg(devices={"a_b": "ok"}),
                  cfg(remotes={"192.0.2.10": "$(id)"}), cfg(remotes={"192.0.2.10": ""}), cfg(provider="other"),
                  cfg(volume="loud"), cfg(phone={"port": 70000}),
                  # 校验不放过结尾换行
                  cfg(devices={"laptop\n": "ok"}), cfg(devices={"laptop": "my-laptop\n"}),
                  cfg(remotes={"192.0.2.10": "box\n"})):
            assert rejected(c), c
        assert "openai.api_key" in rejected(cfg(openai={"base_url": "https://x", "model": "m", "voice": "v"}))
        assert "空格" in rejected(cfg(openai={"base_url": "https://x", "api_key": "a b", "model": "m", "voice": "v"}))
        for url in ("https://x\ny", "https://x y", "ftp://x"):    # 地址中间带换行、空格，或不是 http(s)
            assert rejected(cfg(openai={"base_url": url, "api_key": "k-123456", "model": "m", "voice": "v"})), url
        volc = {"endpoint": "https://x", "resource_id": "seed-tts-2.0", "voice": "v"}
        assert "app_id" in rejected({"provider": "volcengine", "volcengine": dict(volc)})
        assert not rejected({"provider": "volcengine", "volcengine": dict(volc, api_key="k-1")})
        assert not rejected({"provider": "volcengine", "volcengine": dict(volc, app_id="1", access_key="k-2")})
        # 手机电台同时在线的连接数上限
        assert not rejected(cfg(phone={"max_listeners": 3}))
        for v in (0, -1, "8", True, 1.5):
            assert rejected(cfg(phone={"max_listeners": v})), v
        assert ALIAS_RE.fullmatch("laptop.local") and not ALIAS_RE.fullmatch("-v") \
            and not ALIAS_RE.fullmatch("") and not ALIAS_RE.fullmatch("box\n")
        assert DEVICE_RE.fullmatch("laptop-2") and not DEVICE_RE.fullmatch("Laptop") \
            and not DEVICE_RE.fullmatch("") and not DEVICE_RE.fullmatch("laptop\n")

    @group
    def 开关参数与会话编号():
        g = globals()
        saved, tmp, sid = (g["RT"], g["CONFIG"]), tempfile.mkdtemp(), str(uuid.uuid4())
        g["RT"], g["CONFIG"] = tmp + "/rt", tmp + "/没有这个配置.json"     # 自检不碰真实的运行目录和配置
        try:
            assert toggle("不是会话编号").startswith("语音播报：没拿到")
            assert toggle(sid + "\n").startswith("语音播报：没拿到")
            out = toggle(sid, "stop")       # 不认识的参数报错，不当成"切换"
            assert "开关没动" in out and not os.path.exists(g["RT"]), out
            real, sys.platform = sys.platform, "linux"
            try:
                assert "macOS" in toggle(sid)
            finally:
                sys.platform = real
            env = os.environ.get("LISTEN_TARGET")
            os.environ["LISTEN_TARGET"] = "box\n"
            try:
                target()
                raise AssertionError("带换行的 LISTEN_TARGET 被接受了")
            except ConfigError:
                pass
            finally:
                if env is None:
                    del os.environ["LISTEN_TARGET"]
                else:
                    os.environ["LISTEN_TARGET"] = env
        finally:
            g["RT"], g["CONFIG"] = saved
            shutil.rmtree(tmp, ignore_errors=True)

    @group
    def 密钥抹除():
        g = globals()
        saved, tmp = (g["CONFIG"], list(SECRETS)), tempfile.mkdtemp()

        def load(key):
            g["CONFIG"] = tmp + "/c.json"
            with open(g["CONFIG"], "w") as f:
                json.dump({"provider": "openai", "openai": {"api_key": key}}, f)
            read_config()
        try:
            load("k")       # 很短的密钥不做抹除：否则正常文字被搅乱，还能反推出密钥
            text = "nodename nor servname provided, or not known"
            assert scrub(text) == text, scrub(text)
            load("LONG-SECRET-123456")
            assert scrub("a LONG-SECRET-123456 b") == "a *** b"
        finally:
            g["CONFIG"] = saved[0]
            SECRETS[:] = saved[1]
            shutil.rmtree(tmp, ignore_errors=True)

    @group
    def 进程号文件核对():
        tmp = tempfile.mkdtemp()
        f = tmp + "/x.pid"
        try:
            def put(text):
                with open(f, "w") as fh:
                    fh.write(text)
            put(str(os.getpid()))
            assert pid_of(f, "listen.py") == os.getpid()
            assert pid_of(f, "listen.py run") == 0          # 进程活着但不是播报进程：不算
            dead = subprocess.Popen(["true"])
            dead.wait()
            for text in (str(dead.pid), "0", "-1", "abc", ""):
                put(text)
                assert pid_of(f, "listen.py") == 0, text
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @group
    def 令牌生成():
        g = globals()
        saved, tmp = g["RT"], tempfile.mkdtemp()
        g["RT"] = tmp
        try:
            t = token()
            assert TOKEN_RE.fullmatch(t) and token() == t        # 生成一次后固定
            for bad in ("", "abc", t[:8], t + "\n" + t):           # 空、过短、内容不对：重新生成
                with open(tmp + "/token", "w") as f:
                    f.write(bad)
                assert TOKEN_RE.fullmatch(token()) and token() != bad.strip()
            t = token()
            os.chmod(tmp + "/token", 0)     # 读不了（不是"没有"也不是"不对"）：不能悄悄换掉，否则在听的设备全被断开
            try:
                token()
                changed = True
            except OSError:
                changed = False
            os.chmod(tmp + "/token", 0o600)
            assert not changed and token() == t, "读不了令牌文件时令牌被换掉了"
            assert token(new=True) != t
        finally:
            g["RT"] = saved
            shutil.rmtree(tmp, ignore_errors=True)

    @group
    def 日志大小上限():
        g = globals()
        saved, tmp = g["RT"], tempfile.mkdtemp()
        g["RT"] = tmp
        try:
            for _ in range(2):      # 超过上限就把旧的改名为 .1，只留一份旧的
                with open(tmp + "/listen.log", "w") as f:
                    f.write("x" * (2 << 20))
                log("新的一行")
            assert sorted(os.listdir(tmp)) == ["listen.log", "listen.log.1"], os.listdir(tmp)
            assert os.path.getsize(tmp + "/listen.log") < 200 and os.path.getsize(tmp + "/listen.log.1") > (1 << 20)
        finally:
            g["RT"] = saved
            shutil.rmtree(tmp, ignore_errors=True)

    @group
    def 电台名额():
        slots = Slots(4)
        assert slots.take("a") and slots.take("a") and not slots.take("a")     # 同一地址未验证的最多 2 个
        assert slots.take("b")                  # 一个地址占满了自己的份额，别的地址照样能进来
        slots.verified("a", "r1")               # 通过令牌校验的连接不再算"未验证"
        assert slots.take("c") and not slots.take("d")      # 总数到上限（4）：谁都进不来了
        slots.release("a", "r1")                # 已验证的那条结束
        assert slots.take("a") and not slots.take("a")
        slots.release("b", "r2")                # 未验证的那条结束：b 的份额也还回去
        assert slots.take("b")

    @group
    def 明文地址提醒():
        assert plain_http("http://192.0.2.10/v1") and plain_http("http://tts.example.com")
        assert not plain_http("https://api.example.com") and not plain_http("http://127.0.0.1:18951/v1") \
            and not plain_http("http://localhost:1") and not plain_http("http://[::1]:8/")

    bad = []
    for fn in groups:
        try:
            fn()
            print("通过   " + fn.__name__)
        except Exception as ex:     # 自检的汇总边界：哪一组出什么错都如实报出来，接着跑下一组
            bad.append(fn.__name__)
            print("不通过 %s —— %s: %s" % (fn.__name__, type(ex).__name__, str(ex)[:200]))
    if bad:
        print("自检不通过（%d 组）：%s" % (len(bad), "、".join(bad)))
        sys.exit(1)
    print("自检通过")


def main(cmd, args):
    if cmd == "toggle" and args:
        print(toggle(args[0], " ".join(args[1:])))
    elif cmd == "run" and len(args) == 2 and args[1].isdigit():
        run(args[0], int(args[1]))
    elif cmd == "phone":
        phone()
    elif cmd == "status":
        status()
    elif cmd == "doctor":
        sys.exit(0 if doctor() else 1)
    elif cmd == "token" and args == ["--new"]:
        token(new=True)
        print("收听令牌已更换：旧地址立刻失效，正在收听的设备会在一秒内被断开。")
        try:
            print("新的收听地址：%s" % phone_url())
        except ConfigError as ex:
            print("新的收听地址暂时给不出 —— %s" % describe(ex))
    elif cmd == "say" and args:
        say(" ".join(args))
    elif cmd == "check":
        check()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    try:
        main(cmd, args)
    except Exception as ex:     # 命令的顶层：任何未预期的错误只给一句中文和异常类型，不出异常栈和绝对路径
        print({"toggle": "语音播报：没开成 —— ", "say": "试音失败 —— "}.get(cmd, "语音播报：出错了 —— ") + describe(ex))
        sys.exit(0 if cmd == "toggle" else 1)    # /listen 的结果照常显示给用户，不当成命令出错
