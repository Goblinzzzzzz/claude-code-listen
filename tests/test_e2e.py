#!/usr/bin/python3
"""claude-code-listen 端到端测试。

用法：/usr/bin/python3 tests/test_e2e.py        （大约 4 分钟）

只用标准库和 ffmpeg/ffprobe。测试里起一个本地假语音服务，同时模拟"兼容 OpenAI 的语音接口"和
"火山引擎单向流式接口"两种形状；全程使用临时的运行目录、配置文件和 18950 以上的端口，
不读取任何真实密钥与真实配置，也不会真的出声：播报大多送到手机电台，再由测试连上电台录音分析；
走本机和远程电脑的那几项，用 PATH 最前面的假 afplay、假 ssh、假 tmux 代替真程序。
每项打印 通过/不通过；有不通过时退出码为 1。每项测试单独隔离，前面出错不影响后面的项照常跑完。
排查时可以只跑其中几项：E2E_ONLY=t06_playback,t16_keep /usr/bin/python3 tests/test_e2e.py
结束后清理自己造的所有东西（含 ~/.claude/projects/ 下名字以 -listen-e2e- 开头的假会话目录）。
"""
import base64
import glob
import http.client
import itertools
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import struct
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTEN = ROOT + "/listen.py"
sys.dont_write_bytecode = True
sys.path.insert(0, ROOT)
import listen   # noqa: E402  只借用它的 mp3 帧解析来裁掉录音末尾的残帧
PY = sys.executable
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
SECRET = "SECRET-KEY-DO-NOT-LEAK-12345"         # 新版鉴权 / OpenAI 的假密钥
SECRET_OLD = "SECRET-ACCESS-DO-NOT-LEAK-67890"  # 火山引擎旧版控制台的假 Access Token
SECRET_ENV = "SECRET-ENV-DO-NOT-LEAK-24680"     # 经环境变量 LISTEN_API_KEY 提供的假密钥
SECRETS = (SECRET, SECRET_OLD, SECRET_ENV)
CUE = "另一个对话："
BURST = 1.0     # 电台给新连上的听众先补约一秒静音：录音第 p 秒 ≈ 首字节到达后第 p-BURST 秒发出

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"     # 本机地址不走系统代理
TMP = tempfile.mkdtemp(prefix="listen-e2e-")
RT = TMP + "/rt"
PROJ = os.path.expanduser("~/.claude/projects/-listen-e2e-%s" % uuid.uuid4().hex[:8])
FAKEBIN, TMUX_LOG, FAKE_DIR = TMP + "/bin", TMP + "/tmux.log", TMP + "/fake"
results, OUT, PIDS, SESSIONS, HOMES, PORTS = [], [], set(), [], [], set()
MODE = {"openai": "ok", "volc": "ok"}
REQS, BAD, TONES, STOLEN = [], [], {}, []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("通过   " if cond else "不通过 ") + name + ("  —— " + str(detail) if detail else ""), flush=True)


def free_port():
    for p in range(18950, 19100):
        if p in PORTS:
            continue
        with socket.socket() as s:
            try:
                s.bind(("0.0.0.0", p))
            except OSError:
                continue
        PORTS.add(p)
        return p
    sys.exit("18950~19099 之间找不到空闲端口")


RADIO_PORT, TTS_PORT, STEAL_PORT = free_port(), free_port(), free_port()


# ---------------- 假语音服务 ----------------

def tone_secs(text):
    return max(0.6, 0.12 * len(text))      # 时长与字数成正比，方便判断"念了多少"


def tone(text, rate=24000, ch=1):
    key = (round(tone_secs(text), 2), rate, ch)
    if key not in TONES:
        TONES[key] = subprocess.run(
            [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=%d" % rate,
             "-t", "%.2f" % key[0], "-ar", str(rate), "-ac", str(ch), "-b:a", "64k" if ch == 1 else "128k",
             "-f", "mp3", "pipe:1"], capture_output=True, check=True).stdout
    return TONES[key]


def masked(key):
    return key[:4] + "****" + key[-4:]


def b64(text):
    return base64.b64encode(text.encode()).decode()


class FakeTTS(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, code, body=b"", ctype="audio/mpeg"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self):     # 把请求转去另一台主机：看程序会不会把密钥头一并带过去
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:%d/stolen" % STEAL_PORT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            j = json.loads(body)
        except ValueError:
            j = None
        problems = []
        if not (self.headers.get("User-Agent") or "").startswith("claude-code-listen/"):
            problems.append("User-Agent 不对")
        if self.headers.get("Content-Type") != "application/json":
            problems.append("Content-Type 不对")
        if self.headers.get("X-Test-Extra") != "yes":
            problems.append("extra_headers 没带上")
        if self.path == "/v1/audio/speech":
            self.openai(j, problems)
        elif self.path == "/api/v3/tts/unidirectional":
            self.volc(j, problems)
        else:
            self.reply(404)

    def openai(self, j, problems):
        auth = self.headers.get("Authorization") or ""
        key = auth[7:] if auth.startswith("Bearer ") else ""
        if key not in SECRETS:
            problems.append("Authorization 不对")
        if not isinstance(j, dict) or set(j) != {"model", "voice", "input", "response_format", "speed"}:
            problems.append("请求体字段不对")
        elif (j["model"], j["voice"], j["response_format"], j["speed"]) != ("test-model", "test-voice", "mp3", 1.0):
            problems.append("请求体取值不对")
        text = j.get("input", "") if isinstance(j, dict) else ""
        REQS.append({"api": "openai", "text": text, "key": key})
        if problems:
            BAD.append(("openai", problems))
            return self.reply(400, b"bad request", "text/plain")
        mode = MODE["openai"]
        if mode == "redirect":
            return self.redirect()
        if mode == "garbage":       # 状态行是乱码，里面夹着服务端的文字
            self.wfile.write(b"NOTHTTP SERVERTEXT-GARBAGE\r\n\r\n")
            self.close_connection = True
            return
        if mode == "drip":          # 回了 200 之后每 5 秒才滴一个字节，迟迟不结束（每次收数据都不超过 30 秒）
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            for _ in range(40):
                try:
                    self.wfile.write(b"\xff")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(5)
            return
        if mode == "empty":         # 有的服务对无效音色回 200 加空正文
            return self.reply(200, b"")
        if mode == "echo401":       # 故意在错误正文里回显完整密钥，看程序会不会把它带进日志
            return self.reply(401, json.dumps({"error": {"message": "Incorrect API key: " + key}}).encode(),
                              "application/json")
        self.reply(200, tone(text, 44100, 2) if mode == "stereo" else tone(text))

    def volc(self, j, problems):
        h = self.headers
        key = h.get("X-Api-Key") or ""
        if key:
            if key not in SECRETS:
                problems.append("X-Api-Key 不对")
            if h.get("X-Api-App-Id") or h.get("X-Api-Access-Key"):
                problems.append("用了新版鉴权却还带着旧版的头")
        elif h.get("X-Api-App-Id") == "test-app" and h.get("X-Api-Access-Key") == SECRET_OLD:
            key = SECRET_OLD
        else:
            problems.append("鉴权头不对")
        if h.get("X-Api-Resource-Id") != "seed-tts-2.0":
            problems.append("X-Api-Resource-Id 不对")
        try:
            uuid.UUID(h.get("X-Api-Request-Id") or "")
        except ValueError:
            problems.append("X-Api-Request-Id 不是 uuid")
        rp = j.get("req_params") if isinstance(j, dict) and set(j) == {"req_params"} else None
        ap = rp.get("audio_params") if isinstance(rp, dict) and set(rp) == {"text", "speaker", "audio_params"} \
            else None
        if not isinstance(ap, dict) or rp["speaker"] != "test-speaker" or ap != {
                "format": "mp3", "sample_rate": 24000, "speech_rate": 10}:   # 配置里 speed=1.1 → 10
            problems.append("请求体结构不对")
        text = rp.get("text", "") if isinstance(rp, dict) else ""
        REQS.append({"api": "volc", "text": text, "key": key})
        if problems:
            BAD.append(("volc", problems))
            return self.reply(400, b'{"code": 45000000, "message": "bad request"}', "application/json")
        if MODE["volc"] == "redirect":
            return self.redirect()
        if MODE["volc"] == "http401":   # HTTP 层报错，正文里回显打了码的密钥
            return self.reply(401, json.dumps({"code": 45000000, "message": "SERVERMSG invalid key " + masked(key)})
                              .encode(), "application/json")
        audio = tone(text)
        step = len(audio) // 3 + 1
        objs = [{"code": 0, "message": "", "data": base64.b64encode(audio[i:i + step]).decode()}
                for i in range(0, len(audio), step)]
        if MODE["volc"] == "errcode":   # 错误消息里故意夹带密钥原文、打码形式、base64 形式和请求文本
            objs = objs[:1] + [{"code": 45000001, "data": None, "message": "SERVERMSG denied key=%s masked=%s b64=%s "
                                "text=%s" % (key, masked(key), b64(key), text)}]
        else:
            objs.append({"code": 20000000, "message": "ok", "data": None})
        stream = "".join(json.dumps(o, ensure_ascii=False) for o in objs).encode()  # 首尾相接，不加换行
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(0, len(stream), 700):    # 按固定长度切块：JSON 对象会被拆在两块之间
            part = stream[i:i + 700]
            self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


class Steal(BaseHTTPRequestHandler):
    """跳转的目的地：记下收到的请求（不该收到任何请求），并回一段音频，看程序会不会因此报"成功"。"""

    def log_message(self, *args):
        pass

    def answer(self):
        STOLEN.append(self.path)
        body = tone("被转走的请求")
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = answer


# ---------------- 工具 ----------------

def write_cfg(name, c, mode=0o600):
    path = "%s/%s.json" % (TMP, name)
    with open(path, "w") as f:
        f.write(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    os.chmod(path, mode)
    return path


def openai_cfg(port=None, cue=CUE, **sec):
    s = {"base_url": "http://127.0.0.1:%d/v1/" % TTS_PORT, "api_key": SECRET, "model": "test-model",
         "voice": "test-voice", "speed": 1.0, "extra_headers": {"X-Test-Extra": "yes"}}
    s.update(sec)
    return {"provider": "openai", "volume": 0, "switch_cue": cue, "openai": s,
            "phone": {"port": port or RADIO_PORT, "advertise_host": "192.0.2.10"}, "devices": {}, "remotes": {}}


def volc_cfg(**sec):
    s = {"endpoint": "http://127.0.0.1:%d/api/v3/tts/unidirectional" % TTS_PORT, "api_key": SECRET, "app_id": "",
         "access_key": "", "resource_id": "seed-tts-2.0", "voice": "test-speaker", "speed": 1.1,
         "extra_headers": {"X-Test-Extra": "yes"}}
    s.update(sec)
    return {"provider": "volcengine", "volume": 0, "switch_cue": CUE, "volcengine": s,
            "phone": {"port": RADIO_PORT, "advertise_host": "192.0.2.10"}, "devices": {}, "remotes": {}}


BASE = {k: v for k, v in os.environ.items() if not k.startswith("LISTEN_") and k != "TMUX"}   # 不混进真实设置
BASE.update(LISTEN_RT=RT, LISTEN_TARGET="phone", LISTEN_VOLUME="0", LISTEN_PHONE_IDLE="4",
            PATH=FAKEBIN + ":" + os.environ.get("PATH", ""), FAKE_TMUX_LOG=TMUX_LOG, FAKE_DIR=FAKE_DIR)


def cli(*args, cfg=None, **env):
    e = dict(BASE, **env)
    if cfg:
        e["LISTEN_CONFIG"] = cfg
    r = subprocess.run([PY, LISTEN] + list(args), capture_output=True, text=True, env=e, timeout=90)
    OUT.append(r.stdout + r.stderr)
    return (r.stdout + r.stderr).strip()


def read_pid(path):
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return 0


def ours(pid):
    """这个进程号还活着、并且确实是 listen.py 的进程。"""
    return pid > 0 and "listen.py" in subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                                     capture_output=True, text=True).stdout


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pidfiles(rt=RT):
    return sorted(x for x in os.listdir(rt) if x.endswith(".pid")) if os.path.isdir(rt) else []


def log_text(rt=RT):
    try:
        with open(rt + "/listen.log") as f:
            return f.read()
    except OSError:
        return ""


def tmux_lines():
    try:
        with open(TMUX_LOG) as f:
            return [line for line in f.read().splitlines() if line.startswith("display-message")]
    except OSError:
        return []


def fake_set(name, value):
    with open(FAKE_DIR + "/" + name, "w") as f:
        f.write(value)


def fake_clear(name):
    try:
        os.remove(FAKE_DIR + "/" + name)
    except OSError:
        pass


def fake_log(name):
    try:
        with open(FAKE_DIR + "/" + name) as f:
            return f.read().splitlines()
    except OSError:
        return []


def wait_for(fn, secs, step=0.1):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())


def tts_files():
    return sum(len(os.listdir(d)) for d in glob.glob(RT + "/tts-*"))


def radio_up(port=RADIO_PORT):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_radio():
    for _ in range(100):
        pid = read_pid(RT + "/phone.pid")
        if pid and radio_up():
            PIDS.add(pid)
            return True
        time.sleep(0.1)
    return False


def http_get(port, path, timeout=3):
    """→ (状态码, Server 响应头, 正文开头)；连不上返回 (0, "", b"")。"""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read(2000) if r.status == 200 else r.read()
        server = r.getheader("Server") or ""
        c.close()
        return r.status, server, body
    except (OSError, http.client.HTTPException):
        return 0, "", b""


def cli_safe(*args, **kw):
    """同 cli，但命令卡住超过时限时返回一句说明，而不是让整项测试中途出错。"""
    try:
        return cli(*args, **kw)
    except subprocess.TimeoutExpired:
        return "（命令 90 秒都没有结束）"


def spool_files(rt):
    try:
        return [x for x in os.listdir(rt + "/phone-spool") if x.endswith((".mp3", ".beep"))]
    except OSError:
        return []


def threads_of(pid):
    out = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True).stdout
    return max(0, len(out.strip().splitlines()) - 1)


def side_radio(name, port, ulimit=None, token_text=None, stale=False, idle="600"):
    """另起一个独立的手机电台（自己的运行目录、配置和端口），专门用来做会把电台弄坏的实验。"""
    rt = "%s/rt-%s" % (TMP, name)
    cfg = write_cfg("cfg-" + name, openai_cfg(port=port))
    if token_text is not None or stale:
        os.makedirs(rt, mode=0o700, exist_ok=True)
    if token_text is not None:
        with open(rt + "/token", "w") as f:
            f.write(token_text)
    if stale:   # 进程早已不在的会话进程号文件，而且是一分钟前留下的
        dead = subprocess.Popen(["true"])
        dead.wait()
        path = "%s/%s.pid" % (rt, uuid.uuid4())
        with open(path, "w") as f:
            f.write(str(dead.pid))
        os.utime(path, (time.time() - 60, time.time() - 60))
    cmd = [PY, LISTEN, "phone"]
    if ulimit:
        cmd = ["/bin/bash", "-c", 'ulimit -n %d && exec "$@"' % ulimit, "bash"] + cmd
    env = dict(BASE, LISTEN_RT=rt, LISTEN_CONFIG=cfg, LISTEN_PHONE_IDLE=idle)
    out = open("%s/side-%s.out" % (TMP, name), "w")
    p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    PIDS.add(p.pid)
    wait_for(lambda: read_pid(rt + "/phone.pid") or p.poll() is not None, 6)
    return p, rt, cfg, env


def stop_side(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


class Session:
    def __init__(self, rt=RT):
        self.sid, self.rt = str(uuid.uuid4()), rt
        self.path = "%s/%s.jsonl" % (PROJ, self.sid)
        open(self.path, "w").close()
        SESSIONS.append(self)

    def add_raw(self, lines):
        with open(self.path, "a") as f:
            f.write("".join(line + "\n" for line in lines))

    def add(self, e):
        self.add_raw([json.dumps(e, ensure_ascii=False)])

    @staticmethod
    def reply(text):
        return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
                          ensure_ascii=False)

    def say(self, text):
        self.add_raw([self.reply(text)])

    def say_many(self, texts):      # 一次写进去，播报进程在同一轮里读到
        self.add_raw([self.reply(t) for t in texts])

    def user(self, text):
        self.add({"type": "user", "message": {"content": text}})

    def pid(self):
        return read_pid("%s/%s.pid" % (self.rt, self.sid))

    def on(self, cfg, **env):
        out = cli("toggle", self.sid, "on", cfg=cfg, LISTEN_RT=self.rt, **env)
        if self.pid():
            PIDS.add(self.pid())
            # 播报进程起来时把"记录文件当前长度"当作起点，之前写进去的不念：等它写出"开始播报"再往下走
            wait_for(lambda: "开始播报 会话=%s" % self.sid in log_text(self.rt), 5)
        return out

    def off(self):
        return cli("toggle", self.sid, "off", LISTEN_RT=self.rt)


class Recorder(threading.Thread):
    """连上手机电台录 secs 秒。t0 是首字节到达的时刻，之后的事件都按相对 t0 的秒数安排。"""

    def __init__(self, name, secs):
        super().__init__(daemon=True)
        self.path, self.secs, self.started = "%s/%s.mp3" % (TMP, name), secs, threading.Event()
        self.t0 = self.t1 = 0

    def run(self):
        token = open(RT + "/token").read().strip()
        r = urllib.request.urlopen("http://127.0.0.1:%d/%s.mp3" % (RADIO_PORT, token), timeout=10)
        with open(self.path, "wb") as f:
            f.write(r.read(1))
            self.t0 = time.time()
            self.started.set()
            while time.time() - self.t0 < self.secs:
                f.write(r.read(1024))
        self.t1 = time.time()
        r.close()

    def begin(self):
        self.start()
        if not self.started.wait(10):
            raise RuntimeError("连不上手机电台")
        return self

    def at(self, sec, fn):
        while time.time() - self.t0 < sec:
            time.sleep(0.01)
        fn()

    def finish(self):
        self.join(self.secs + 15)
        wall = self.t1 - self.t0
        # 测试到点就掐断连接，最后一帧往往只录到一半，ffmpeg 会对这个残片报错；真实的播放器不会遇到。
        # 所以截掉末尾不完整的那一帧，同时确认其余部分是一帧接一帧、中间没有多余的字节。
        with open(self.path, "rb") as f:
            data = f.read()
        whole = sum(len(fr) for fr, _ in listen.frames(data))
        self.gap = data[:whole] != b"".join(fr for fr, _ in listen.frames(data))
        self.cut = len(data) - whole
        with open(self.path, "wb") as f:
            f.write(data[:whole])
        sd = subprocess.run([FFMPEG, "-nostdin", "-i", self.path, "-af", "silencedetect=noise=-45dB:d=0.4",
                             "-f", "null", "-"], capture_output=True, text=True).stderr
        t = re.findall(r"time=(\d+):(\d+):([\d.]+)", sd)
        self.dur = int(t[-1][0]) * 3600 + int(t[-1][1]) * 60 + float(t[-1][2]) if t else 0
        ss = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", sd)]
        se = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", sd)]
        self.voiced = [(round(e - BURST, 2), round((ss[i + 1] if i + 1 < len(ss) else self.dur) - BURST, 2))
                       for i, e in enumerate(se)]
        self.voiced = [(a, b) for a, b in self.voiced if b - a > 0.2]
        self.errors = subprocess.run([FFMPEG, "-v", "error", "-i", self.path, "-f", "null", "-"],
                                     capture_output=True, text=True).stderr.strip()
        print("    录音 %s：时长 %.1f 秒，墙钟 %.1f 秒，有声时段 %s" % (os.path.basename(self.path), self.dur, wall,
                                                       self.voiced), flush=True)
        return wall

    def has(self, a, b):
        return any(s < b and e > a for s, e in self.voiced)

    def total(self, a, b):
        return sum(max(0, min(e, b) - max(s, a)) for s, e in self.voiced)

    def stop_after(self, t):
        """t 时刻正在念的那段在什么时候停下（相对 t 的秒数）；t 前半秒没在念就返回 None。"""
        seg = [e for s, e in self.voiced if s < t - 0.5 and e > t - 0.5]
        return seg[0] - t if seg else None


class SpoolWatch(threading.Thread):
    """盯着排队目录，记下同一时刻最多排着几段音频（排队念的话最多一段）。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.most, self.going = 0, True

    def run(self):
        while self.going:
            try:
                n = len([x for x in os.listdir(RT + "/phone-spool") if x.endswith((".mp3", ".beep"))])
            except OSError:
                n = 0
            self.most = max(self.most, n)
            time.sleep(0.02)


class Listener(threading.Thread):
    """一个一直在收听电台的设备：记下收到了多少字节、连接是不是被断开了。"""

    def __init__(self, port, token):
        super().__init__(daemon=True)
        self.port, self.token, self.bytes, self.status, self.closed, self.going = port, token, 0, 0, False, True

    def run(self):
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", "/%s.mp3" % self.token)
            r = c.getresponse()
            self.status = r.status
            while self.going:
                b = r.read(512)
                if not b:
                    break
                self.bytes += len(b)
        except (OSError, http.client.HTTPException):
            pass
        self.closed = True


def common_checks(tag, rec, wall):
    check("%s：录音无解码错误（一帧接一帧，无多余字节）" % tag, not rec.errors and not rec.gap,
          rec.errors[:200] or "截掉末尾残帧 %d 字节" % rec.cut)
    check("%s：录音时长与墙钟相差不超过 10%%" % tag, abs(rec.dur - wall) / wall <= 0.10,
          "录音 %.1f 秒 / 墙钟 %.1f 秒" % (rec.dur, wall))


# ---------------- 原有各项 ----------------

def test_selfcheck():
    out = cli("check")
    check("1. check 自检通过", out.endswith("自检通过") and "Traceback" not in out,
          "\n".join(line for line in out.splitlines() if line.startswith(("不通过", "自检"))))


def test_bad_configs():
    sid = str(uuid.uuid4())
    cases = [
        ("缺配置文件", TMP + "/no-such-config.json", "找不到配置文件"),
        ("不是 JSON", write_cfg("bad-json", "{ \"provider\": "), "不是合法的 JSON"),
        ("provider 不认识", write_cfg("bad-provider", dict(openai_cfg(), provider="foo")), "provider"),
        ("没填 openai.api_key", write_cfg("bad-key", openai_cfg(api_key="")), "openai.api_key"),
        ("火山没填鉴权", write_cfg("bad-volc", volc_cfg(api_key="")), "app_id"),
        ("ssh 别名不合法", write_cfg("bad-alias", dict(openai_cfg(), devices={"laptop": "-oProxyCommand"})),
         "ssh 别名"),
        ("设备名不合法", write_cfg("bad-device", dict(openai_cfg(), devices={"My Laptop": "laptop"})), "设备名"),
    ]
    for name, path, words in cases:
        out = cli("toggle", sid, "on", cfg=path)
        clean_exit = "Traceback" not in out and not pidfiles()
        check("7. 配置有问题（%s）：给出中文提示且不留后台进程" % name,
              words in out and "没开成" in out and clean_exit, out.replace(TMP, "<临时目录>"))
    out = cli("doctor", cfg=TMP + "/no-such-config.json")
    check("7. 配置缺失时 doctor 照常给出体检结果", "不存在" in out and "Traceback" not in out)


def test_nudge_and_tip(cfg):
    s = Session()
    os.chmod(cfg, 0o644)
    out = s.on(cfg, LISTEN_TARGET="")
    os.chmod(cfg, 0o600)
    check("没人收听时，开启提示里给出收听地址", "已开启" in out and "http://192.0.2.10:%d/" % RADIO_PORT in out, out)
    check("配置文件权限比 600 宽时给出提醒（不阻止开启）", "chmod 600" in out and "已开启" in out)
    check("手机电台被顺手拉起", wait_radio())
    s.say("这段话没有地方念出来。")
    time.sleep(1.5)
    shown = tmux_lines()
    check("没地方出声时在 tmux 底部提示一次", len(shown) == 1 and "没在收听" in shown[0], shown)
    s.say("第二段话也没有地方念。")
    time.sleep(1.5)
    check("30 秒内不重复提示", len(tmux_lines()) == 1, tmux_lines())
    check("没地方出声的话不会送去合成", not [r for r in REQS if "没有地方念" in r["text"]])
    check("关闭播报", "已关闭" in s.off())


def test_openai_interrupt(cfg, s1):
    out = s1.on(cfg)
    check("openai：开启播报，出声位置是手机", "已开启" in out and "手机" in out, out)
    wait_radio()
    rec = Recorder("openai", 28).begin()
    rec.at(2, lambda: s1.say("手机电台测试，第一句话。"))
    rec.at(6, lambda: s1.say("这是一段比较长的回复，用来测试打断。" * 25))
    rec.at(14, lambda: s1.user("停一下。"))
    rec.at(20, lambda: s1.say("被打断之后的第三句话。"))
    wall = rec.finish()
    check("2. openai：追加回复后流里出现非静音", rec.has(2, 6), rec.voiced)
    check("2. openai：长回复出声了", rec.has(6.5, 13.5))
    stop = rec.stop_after(14)
    check("3. 打断：念长回复时用户发言，1.5 秒内停下", stop is not None and stop < 1.5,
          "停于发言后 %s 秒" % (None if stop is None else round(stop, 2)))
    check("3. 打断：停下之后到下一句之前保持安静", not rec.has(15.5, 19.9))
    check("3. 打断：之后的新回复照常念", rec.has(20, 28))
    common_checks("2. openai", rec, wall)
    check("openai：假服务收到的请求格式全部正确", not [b for b in BAD if b[0] == "openai"], BAD)


def test_volc_multisession(cfg, s1):
    s2, s3 = Session(), Session()
    s2.on(cfg)
    s3.on(cfg)
    wait_radio()
    a = "第二个会话说的话，大概三十个字左右，用来测试排队。"
    b = "第三个会话说的话，也是三十个字左右，用来测试排队。"
    long = "第二个会话的长回复，用来确认别的会话插话不会打断它。" * 6
    z = "第三个会话排队等着的一句话，应该被它自己的用户打断。"
    watch = SpoolWatch()
    watch.start()
    rec = Recorder("volc", 28).begin()
    status = {}
    rec.at(1.5, lambda: s2.say(a))
    rec.at(1.6, lambda: s3.say(b))
    rec.at(10, lambda: status.setdefault("out", cli("status", cfg=cfg)))
    rec.at(12, lambda: s2.say(long))
    rec.at(14, lambda: s3.say(z))
    rec.at(15.5, lambda: s3.user("先别说了。"))
    rec.at(19, lambda: s2.user("停。"))
    wall = rec.finish()
    watch.going = False
    watch.join()
    # 上一个说话的是别的会话时，先念一句提示：本段开头两个会话各换一次人（前面最后说话的是 openai 那个会话）。
    # 段与段之间不到 0.4 秒的空隙会被算成有声，所以容差放 1.5 秒；少念一整段会差 3 秒，仍分得清。
    want = tone_secs(a) + tone_secs(b) + 2 * tone_secs(CUE)
    got = rec.total(1.5, 11.9)
    check("2. 火山引擎：追加回复后流里出现非静音", rec.has(1.5, 6))
    check("4. 多会话：两个会话几乎同时说话，两段都念出来（总有声时长≈两段加换人提示）", abs(got - want) <= 1.5,
          "有声 %.1f 秒 / 预期 %.1f 秒" % (got, want))
    check("4. 多会话：依次进行不叠加（排队目录里同一时刻最多一段音频）", watch.most == 1, "最多 %d 段" % watch.most)
    check("4. 多会话：换会话时念了提示语", any(r["text"] == CUE for r in REQS if r["api"] == "volc"))
    check("多会话：别的会话里用户发言，不打断正在念的会话", rec.has(16.5, 18.9))
    stop = rec.stop_after(19)
    check("多会话：用户在哪个会话发言就停哪个，1.5 秒内停下", stop is not None and stop < 1.5,
          "停于发言后 %s 秒" % (None if stop is None else round(stop, 2)))
    check("多会话：被自己用户打断的排队内容不再念", not rec.has(20.6, 28))
    common_checks("2. 火山引擎", rec, wall)
    check("火山引擎：假服务收到的请求格式全部正确", not [x for x in BAD if x[0] == "volc"], BAD)
    out = status.get("out", "")
    check("status 显示会话、电台、收听设备与地址",   # 此时 openai 那个会话也还开着，共 3 个
          "3 个" in out and all(s.sid in out for s in (s1, s2, s3)) and "手机电台：在运行" in out
          and "1 台" in out and "http://192.0.2.10:%d/" % RADIO_PORT in out, out)
    s2.off()
    s3.off()
    return s2


def test_failures(cfg_openai, cfg_volc, s1, s2):
    rec = Recorder("fail-openai", 16).begin()
    rec.at(1.5, lambda: (MODE.update(openai="empty"), s1.say("空正文测试。")))
    rec.at(5, lambda: (MODE.update(openai="echo401"), s1.say("错误密钥测试。")))
    rec.at(9, lambda: (MODE.update(openai="ok"), s1.say("恢复之后的这句话应该能念出来。")))
    rec.finish()
    log = log_text()
    check("5. 失败路径（空正文）：日志里有失败记录", "合成失败" in log and "0 字节" in log)
    check("5. 失败路径（空正文）：响了提示音", 0 < rec.total(1.5, 4.9) < 0.7, rec.total(1.5, 4.9))
    check("5. 失败路径（HTTP 401）：日志里有失败记录", "HTTP 401" in log)
    check("5. 失败路径（HTTP 401）：响了提示音", 0 < rec.total(5, 8.9) < 0.7, rec.total(5, 8.9))
    check("5. 失败之后恢复正常仍能念", rec.total(9, 16) > 1.2, rec.total(9, 16))
    check("5. 失败之后 openai 播报进程不崩", ours(s1.pid()))
    s2.on(cfg_volc)
    rec = Recorder("fail-volc", 12).begin()
    rec.at(1.5, lambda: (MODE.update(volc="errcode"), s2.say("错误码测试。")))
    rec.at(5, lambda: (MODE.update(volc="ok"), s2.say("火山引擎恢复正常之后的这句话。")))
    rec.finish()
    check("5. 失败路径（火山错误 code）：日志里有失败记录", "code=45000001" in log_text())
    check("5. 火山错误 code 之后恢复正常仍能念", rec.total(5, 12) > 1.2, rec.total(5, 12))
    check("5. 失败之后火山播报进程不崩", ours(s2.pid()))
    s2.off()


def test_survive(cfg):
    """登记的 Claude 进程退出后，播报进程继续干；记录文件超过空闲时长没更新才退出。"""
    s = Session()
    sleeper = subprocess.Popen(["sleep", "1"])
    with open(RT + "/listen.log", "a") as logf:
        p = subprocess.Popen([PY, LISTEN, "run", s.sid, str(sleeper.pid)], stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=logf, start_new_session=True,
                             env=dict(BASE, LISTEN_CONFIG=cfg, LISTEN_IDLE="8"))
    PIDS.add(p.pid)
    with open("%s/%s.pid" % (RT, s.sid), "w") as f:
        f.write(str(p.pid))
    rec = Recorder("survive", 12).begin()
    sleeper.wait()
    rec.at(3, lambda: None)
    alive_after = ours(p.pid)
    rec.at(3.2, lambda: s.say("Claude 进程换了之后，这句话照样要念出来。"))
    rec.at(9, lambda: None)
    alive_idle = ours(p.pid)    # 距最后一次更新约 6 秒，还没到 8 秒的空闲时长
    rec.finish()
    check("存活：登记的 Claude 进程退出后，播报进程仍在", alive_after)
    check("存活：之后追加的回复仍能出声", rec.has(3.2, 10), rec.voiced)
    check("存活：没到空闲时长前不退出", alive_idle)
    gone = wait_for(lambda: p.poll() is not None, 8)
    check("8. 进程不在且记录文件超过空闲时长无更新后自行退出，并注销进程号文件",
          gone and not os.path.exists("%s/%s.pid" % (RT, s.sid)))


def test_format(s1):
    rec = Recorder("format", 12).begin()
    rec.at(1.5, lambda: (MODE.update(openai="stereo"), s1.say("立体声格式测试，这一句是四十四点一千赫兹。")))
    rec.finish()
    MODE["openai"] = "ok"
    fr = json.loads(subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
                                    "frame=nb_samples,channels", "-of", "json", rec.path],
                                   capture_output=True, text=True).stdout or "{}").get("frames", [])
    runs = [(k, len(list(g))) for k, g in itertools.groupby((f.get("nb_samples"), f.get("channels")) for f in fr)]
    # 24k 单声道每帧 576 个采样；44.1k 立体声每帧 1152 个采样
    check("手机电台跟随语音格式：从 24k 单声道切到 44.1k 立体声后，之后的静音也保持同一格式",
          len(runs) == 2 and runs[0][0] == (576, 1) and runs[1][0] == (1152, 2) and rec.has(1.5, 8),
          runs)
    check("格式切换处无解码错误（一帧接一帧，无多余字节）", not rec.errors and not rec.gap,
          rec.errors[:200] or "截掉末尾残帧 %d 字节" % rec.cut)


def test_secrets(cfg_openai, cfg_volc):
    outs = {}
    outs["doctor-ok"] = cli("doctor", cfg=cfg_openai)
    MODE["openai"] = "echo401"
    outs["doctor-401"] = cli("doctor", cfg=cfg_openai)
    MODE["openai"] = "ok"
    outs["doctor-volc"] = cli("doctor", cfg=cfg_volc)
    MODE["volc"] = "errcode"
    outs["doctor-volc-err"] = cli("doctor", cfg=cfg_volc)
    MODE["volc"] = "ok"
    old = write_cfg("volc-old", volc_cfg(api_key="", app_id="test-app", access_key=SECRET_OLD))
    outs["doctor-old"] = cli("doctor", cfg=old)
    key_old = REQS[-1]["key"]
    outs["doctor-env"] = cli("doctor", cfg=cfg_openai, LISTEN_API_KEY=SECRET_ENV)
    key_env = REQS[-1]["key"]
    outs["status"] = cli("status", cfg=cfg_openai)
    check("doctor：配置正常时试合成成功，且只报已填写/未填写",
          "试合成：成功" in outs["doctor-ok"] and "api_key：已填写" in outs["doctor-ok"], outs["doctor-ok"])
    check("doctor：服务报 401 时如实报失败", "试合成：失败" in outs["doctor-401"] and "HTTP 401" in outs["doctor-401"])
    check("doctor：火山引擎试合成成功", "试合成：成功" in outs["doctor-volc"], outs["doctor-volc"])
    check("doctor：火山引擎错误 code 如实报出", "45000001" in outs["doctor-volc-err"], outs["doctor-volc-err"])
    check("火山引擎旧版控制台鉴权（app_id + access_key）可用", "试合成：成功" in outs["doctor-old"]
          and key_old == SECRET_OLD, outs["doctor-old"])
    check("环境变量 LISTEN_API_KEY 优先于配置文件", "试合成：成功" in outs["doctor-env"] and key_env == SECRET_ENV
          and "取自环境变量" in outs["doctor-env"])


def test_closing():
    for s in SESSIONS:
        s.off()
    gone = wait_for(lambda: not os.path.exists(RT + "/phone.pid") and not radio_up(), 15)
    check("8. 全部关闭后手机电台空闲退出", gone)
    time.sleep(0.5)
    left = [p for p in PIDS if ours(p)]
    check("8. 无残留进程", not left, left)
    spool = os.listdir(RT + "/phone-spool") if os.path.isdir(RT + "/phone-spool") else []
    tmpdirs = [x for x in os.listdir(RT) if x.startswith("tts-")]
    check("8. 无残留临时文件（排队目录空、临时音频目录已删、无进程号文件）",
          not spool and not tmpdirs and not pidfiles(), (spool, tmpdirs, pidfiles()))


def test_no_leak():
    hits = []
    for i, text in enumerate(OUT):
        hits += ["命令输出 #%d" % i for k in SECRETS if k in text]
    for root, _, files in os.walk(RT):
        for name in files:
            with open(os.path.join(root, name), "rb") as f:
                data = f.read()
            hits += [os.path.relpath(os.path.join(root, name), RT) for k in SECRETS if k.encode() in data]
    check("6. 密钥不外泄：所有命令输出和整个运行目录（含日志）里都搜不到假密钥",
          not hits and OUT and os.path.exists(RT + "/listen.log"), hits or "共查 %d 段命令输出" % len(OUT))


# ---------------- R1 整改：先写的红测试 ----------------

def t01_redirect(cfg_openai, cfg_volc):
    for prov, cfg, name in (("openai", cfg_openai, "openai"), ("volc", cfg_volc, "火山引擎")):
        MODE[prov] = "redirect"
        n = len(STOLEN)
        out = cli("doctor", cfg=cfg)
        MODE[prov] = "ok"
        check("1. %s：语音服务回跳转时拒绝跟随，密钥没被带到另一台主机" % name, len(STOLEN) == n,
              "跳转目的地收到 %d 个请求" % (len(STOLEN) - n))
        check("1. %s：遇到跳转如实报失败，不报成功" % name,
              "跳转" in out and "试合成：失败" in out and "试合成：成功" not in out, out.splitlines()[-1:])


def t02_server_text(cfg_openai, cfg_volc):
    canaries = ("SERVERMSG", masked(SECRET), b64(SECRET), "REPLYCANARY", "SERVERTEXT")
    MODE["volc"] = "errcode"
    out1 = cli("doctor", cfg=cfg_volc)
    MODE["volc"] = "http401"
    out2 = cli("doctor", cfg=cfg_volc)
    MODE["volc"] = "ok"
    MODE["openai"] = "garbage"
    out3 = cli("doctor", cfg=cfg_openai)
    MODE["openai"] = "ok"
    s = Session()
    s.on(cfg_volc)
    before = log_text().count("code=45000001")
    MODE["volc"] = "errcode"
    s.say("REPLYCANARY 这段回复会被服务端原样回显。")
    wait_for(lambda: log_text().count("code=45000001") > before, 6)
    MODE["volc"] = "ok"
    s.off()
    log = log_text()
    leaked = lambda text: [c for c in canaries if c in text]
    check("2. 火山错误码：只给数字 code 和程序自己写的中文说明", "45000001" in out1 and "请求参数无效" in out1,
          out1.splitlines()[-1:])
    check("2. 火山错误码：服务端原文（打码密钥、base64、回显的请求文本）不进 doctor 输出", not leaked(out1),
          leaked(out1))
    check("2. 火山 HTTP 401：只给状态码和数字 code，服务端原文不进输出",
          "HTTP 401" in out2 and "45000000" in out2 and not leaked(out2), out2.splitlines()[-1:])
    check("2. 服务端发来乱码状态行时，乱码原文不进输出", "试合成：失败" in out3 and not leaked(out3),
          out3.splitlines()[-1:])
    check("2. 服务端原文不进播报日志", "code=45000001" in log and not leaked(log), leaked(log))


def t03_slash():
    md = open(ROOT + "/commands/listen.md").read()
    front = md.split("---")[1]
    bang = [line for line in md.splitlines() if line.startswith("!`")]
    check("3. 斜杠命令不接受参数：模板里没有 $ARGUMENTS，也没有 argument-hint",
          "$ARGUMENTS" not in md and "argument-hint" not in front)
    m = re.search(r"^allowed-tools:\s*Bash\((.+):\*\)\s*$", front, re.M)
    cmdline = bang[0][2:-1] if len(bang) == 1 else ""
    fixed = "/usr/bin/python3 ~/.claude/listen/listen.py toggle"
    check("3. allowed-tools 只放行那一条固定命令", bool(m) and m.group(1) == fixed
          and cmdline.startswith(fixed + " "), (m.group(1) if m else None, cmdline))
    pwn = TMP + "/PWNED"
    for evil in ('on"; touch %s; echo "' % pwn, "$(touch %s)" % pwn, "`touch %s`" % pwn):
        line = cmdline.replace("${CLAUDE_SESSION_ID}", str(uuid.uuid4())).replace("$ARGUMENTS", evil) \
            .replace("~/.claude/listen/listen.py", LISTEN)
        subprocess.run(["bash", "-c", line], env=dict(BASE, LISTEN_CONFIG=TMP + "/no-such-config.json"),
                       capture_output=True, timeout=30)
    check("3. 把恶意参数按字面代进命令模板交给 shell，注入的命令也不会执行", not os.path.exists(pwn))


def t04_flood():
    port = free_port()
    p, rt, cfg, env = side_radio("flood", port, ulimit=64)
    tok = open(rt + "/token").read().strip() if os.path.exists(rt + "/token") else ""
    first = Listener(port, tok)
    first.start()
    wait_for(lambda: first.bytes > 0, 3)
    socks = []
    for _ in range(100):        # 一批什么都不发的空闲连接
        s = socket.socket()
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            socks.append(s)
        except OSError:
            s.close()
    time.sleep(1.5)
    up = p.poll() is None
    threads = threads_of(p.pid) if up else -1
    b0 = first.bytes
    time.sleep(1)
    check("4. 电台被一百个空闲连接围攻时不崩溃（文件句柄上限 64）", up, open(TMP + "/side-flood.out").read()[-200:])
    check("4. 电台线程数不随空闲连接数无限增长", 0 < threads <= 20, "线程 %d 个" % threads)
    check("4. 已在收听的设备照常收到音频", first.bytes > b0)
    time.sleep(6)       # 超过读请求头的时限后，电台会关掉这些空闲连接
    st = http_get(port, "/%s.mp3" % tok)[0]
    check("4. 空闲连接超时后，新设备能正常收听", st == 200, st)
    for s in socks:
        s.close()
    first.going = False
    stop_side(p)


def t06_playback(cfg):
    """本机 afplay 与远程 ssh 两条路：PATH 最前面的假 afplay / 假 ssh 模拟成功与失败，不出声。"""
    fake_set("play-secs", "0.3")
    sl = Session()
    sl.on(cfg, LISTEN_TARGET="here")
    n = len(fake_log("afplay.log"))
    sl.say("本机播放测试。")
    wait_for(lambda: len(fake_log("afplay.log")) > n, 8)
    got = fake_log("afplay.log")[n:]
    check("本机出声走 afplay，音量按配置传入（测试里是 0）", bool(got) and got[0].split()[1] == "0.0", got[:1])
    fake_set("afplay-exit", "1")
    sl.say("本机播放失败测试。")
    wait_for(lambda: "这台电脑上播放失败" in log_text(), 6)
    fake_clear("afplay-exit")
    shown = lambda: any("这台电脑" in x and "播放失败" in x for x in tmux_lines())
    wait_for(shown, 3)      # 程序先写日志、再去 tmux 底部提示：两者之间有几十毫秒
    check("6. 本机 afplay 失败：日志里有记录", "这台电脑上播放失败" in log_text())
    check("6. 本机 afplay 失败：tmux 底部有提示", shown(), tmux_lines()[-1:])
    fake_set("afplay-exit", "1")
    r = subprocess.run([PY, LISTEN, "say", "试音失败测试。"], capture_output=True, text=True, timeout=60,
                       env=dict(BASE, LISTEN_CONFIG=cfg, LISTEN_TARGET="here"))
    fake_clear("afplay-exit")
    OUT.append(r.stdout + r.stderr)
    check("6. say 试音时播放失败也如实报错", r.returncode != 0 and "这台电脑上播放失败" in r.stdout, r.stdout.strip())
    sl.off()

    sr = Session()
    sr.on(cfg, LISTEN_TARGET="fakebox")
    n, m = len(fake_log("afplay.log")), len(fake_log("ssh.log"))
    sr.say("远程播放测试。")
    wait_for(lambda: len(fake_log("afplay.log")) > n, 8)
    got = fake_log("afplay.log")[n:]
    f = got[0].split() if got else [""] * 6
    path = f[5] if len(f) > 5 else ""
    sent = fake_log("ssh.log")[m:]
    check("远程出声经 ssh 送到登记的那台电脑", bool(sent) and sent[0].startswith("fakebox "), sent[:1])
    check("12. 推到远程电脑时，音频落在只有本人能读的随机临时目录里",
          bool(re.fullmatch(r"/tmp/listen-tts-[0-9a-f]+-[A-Za-z0-9]{6}/a\.mp3", path)) and f[3:5] == ["600", "700"],
          got[:1])
    wait_for(lambda: path and not os.path.exists(path), 5)
    check("12. 远程播完后临时音频连同目录被删掉", bool(path) and not os.path.exists(os.path.dirname(path)))
    fake_set("ssh-shell", "/bin/csh")
    n = len(fake_log("afplay.log"))
    sr.say("对方登录 shell 是 tcsh 时也要能放。")
    ok = wait_for(lambda: len(fake_log("afplay.log")) > n, 8)
    fake_clear("ssh-shell")
    check("R2-9. 对方电脑的登录 shell 不是 sh 一类（用 tcsh 模拟）时，远程出声照样成立", ok, fake_log("ssh.log")[-1:])
    fake_set("ssh-fail", "1")
    sr.say("远程失败测试。")
    wait_for(lambda: "连不上远程电脑 fakebox" in log_text(), 6)
    fake_clear("ssh-fail")
    wait_for(lambda: any("fakebox" in x for x in tmux_lines()), 3)
    check("6. 远程 ssh 失败：日志里写明是哪台设备", "连不上远程电脑 fakebox" in log_text())
    check("6. 远程 ssh 失败：tmux 底部提示写明是哪台设备", any("fakebox" in x for x in tmux_lines()),
          tmux_lines()[-1:])
    fake_set("play-secs", "20")
    n = len(fake_log("afplay.log"))
    sr.say("远程打断测试，这一段会放很久。")
    wait_for(lambda: len(fake_log("afplay.log")) > n, 8)
    line = fake_log("afplay.log")[n:][:1]
    pid, path = (int(line[0].split()[0]), line[0].split()[5]) if line else (0, "")
    time.sleep(0.5)
    sr.user("停。")
    stopped = wait_for(lambda: not alive(pid), 4) if pid else False
    wait_for(lambda: not os.path.exists(path), 2)
    check("20. 远程打断：远端的播放进程被停掉，临时音频被清掉", stopped and path and not os.path.exists(path),
          (pid, path))
    fake_set("play-secs", "0.3")
    sr.off()


def t07_port_busy():
    port = free_port()
    hold = socket.socket()
    hold.bind(("0.0.0.0", port))
    hold.listen(1)
    rt = TMP + "/rt-busy"
    cfg = write_cfg("cfg-busy", openai_cfg(port=port))
    sid = str(uuid.uuid4())
    out = cli("toggle", sid, "on", cfg=cfg, LISTEN_RT=rt, LISTEN_TARGET="")
    PIDS.add(read_pid("%s/%s.pid" % (rt, sid)))
    check("7. 电台起不来时如实说手机收听不可用及原因，不给打不开的地址",
          "手机收听不可用" in out and "占用" in out and "http://" not in out and "不受影响" in out, out)
    cli("toggle", sid, "off", LISTEN_RT=rt)
    hold.close()


def t08_malformed(cfg):
    s = Session()
    s.on(cfg)
    wait_radio()
    bad = ['{"type":"assistant","message":"RECORDCANARY 字符串"}',
           json.dumps({"type": "assistant", "message": {"content": [
               {"type": "thinking", "thinking": "RECORDCANARY", "signature": None}]}}),
           json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": ["RECORDCANARY", "b"]}]}}),
           json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": None}]}}),
           json.dumps({"type": "assistant", "message": {"content": "RECORDCANARY 裸字符串"}}),
           json.dumps({"type": "user", "message": "RECORDCANARY"}),
           json.dumps({"type": "assistant", "message": None}),
           '"RECORDCANARY 整行是字符串"', "[1, 2, 3]", "12345",
           '{"type":"user","toolUseResult":' + "[" * 5000 + "]" * 5000 + "}",      # 嵌套 5000 层
           json.dumps({"type": "progress", "data": "RECORDCANARY" + "a" * (6 * 1024 * 1024)})]   # 超过 5MB
    rec = Recorder("malformed", 10).begin()
    rec.at(1.0, lambda: s.add_raw(bad))
    rec.at(3.0, lambda: s.say("畸形记录之后的正常回复，应该照常念出来。"))
    rec.finish()
    check("8. 喂入几种畸形记录后播报进程仍在", ours(s.pid()))
    check("8. 之后的正常回复仍能念", rec.has(3.0, 10), rec.voiced)
    check("8. 日志里不带记录正文", "RECORDCANARY" not in log_text())
    check("R2-2. 嵌套过深的一行只记日志并跳过", "跳过一行处理不了的记录：RecursionError" in log_text())
    check("R2-2. 超过 5MB 的一行直接跳过并记日志", "跳过一行过长的记录" in log_text())
    pid = s.pid()
    os.remove(s.path)       # 记录文件没了：进程会退出，但必须先把原因写进日志
    gone = wait_for(lambda: not ours(pid), 5)
    log = log_text()
    check("8. 意外退出时，先把异常类型与位置写进日志", gone and bool(
        re.search(r"意外退出：FileNotFoundError（listen\.py 第 \d+ 行", log)), log.splitlines()[-2:])


def t10_short_key():
    out = cli("doctor", cfg=write_cfg("short-key", openai_cfg(api_key="short1")))
    check("10. 密钥短于 8 位时，配置检查里提醒疑似填错", "过短" in out, out.splitlines()[-3:])


def t11_perms():
    p, rt, cfg, env = side_radio("perm", free_port())
    mode = os.stat(rt).st_mode & 0o777 if os.path.isdir(rt) else 0
    stop_side(p)
    check("11. 运行目录不存在时直接起电台，运行目录建成 700", mode == 0o700, oct(mode))
    dirs = [d for root in (rt, RT) for d, _, _ in os.walk(root)]
    loose = [os.path.relpath(d, TMP) for d in dirs if os.stat(d).st_mode & 0o077]
    check("R2-10. 运行目录里的子目录（排队目录、临时音频目录等）也都是 700", dirs and not loose,
          loose or "查了 %d 个目录" % len(dirs))
    snippet = re.search(r"```zsh\n(.*?)```", open(ROOT + "/README.md").read(), re.S).group(1)
    home = TMP + "/home11"
    os.makedirs(home)
    env = dict(BASE, HOME=home, SSH_TTY="/dev/ttys999", LC_LISTEN_DEVICE="laptop")
    r = subprocess.run(["/bin/zsh", "-f", "-c", "umask 022\n" + snippet], env=env, capture_output=True, text=True,
                       timeout=10)
    run_dir = home + "/.claude/listen/run"
    modes = [oct(os.stat(d).st_mode & 0o777) for d in (run_dir, run_dir + "/tty") if os.path.isdir(d)]
    try:
        content = open(run_dir + "/tty/ttys999").read().strip()
    except OSError:
        content = None
    check("11. README 里的 ~/.zshenv 片段建出的目录也是 700，并记下设备名",
          modes == ["0o700", "0o700"] and content == "laptop", (modes, content, r.stderr.strip()))


def t13_tilde(cfg_openai):
    home = TMP + "/home13"
    os.makedirs(home + "/.cfgdir")
    shutil.copy(cfg_openai, home + "/.cfgdir/config.json")
    out = cli("doctor", cfg=home + "/.cfgdir/config.json", HOME=home)
    check("13. doctor 输出里家目录下的路径显示成 ~/…", "~/.cfgdir/config.json" in out and home not in out,
          out.splitlines()[1:2])


def t15_token():
    port = free_port()
    p, rt, cfg, env = side_radio("token", port, token_text="")
    empty = http_get(port, "/.mp3")[0]
    tok = open(rt + "/token").read().strip() if os.path.exists(rt + "/token") else ""
    check("15. 令牌文件为空时重新生成（至少 80 位随机量），空令牌地址 /.mp3 拿不到音频",
          empty == 404 and bool(re.fullmatch(r"[0-9a-f]{32}", tok)), (empty, len(tok)))
    wrong = [http_get(port, x)[0] for x in ("/%s.mp3" % ("0" * 32), "/", "/%s.mp3" % tok[:8], "/%s" % tok)]
    check("15/20. 错误令牌、截短的令牌、无令牌的路径都拿不到音频", wrong == [404] * 4, wrong)
    status, server, body = http_get(port, "/%s.mp3" % tok)
    check("正确令牌能收听", status == 200 and len(body) > 0, status)
    server404 = http_get(port, "/")[1]
    check("14. 电台的响应头不暴露 Python 版本", "Python" not in server404 and "Python" not in server, server404)
    old = Listener(port, tok)
    old.start()
    wait_for(lambda: old.bytes > 0, 3)
    r = subprocess.run([PY, LISTEN, "token", "--new"], env=env, capture_output=True, text=True, timeout=30)
    OUT.append(r.stdout + r.stderr)
    new = open(rt + "/token").read().strip()
    dropped = wait_for(lambda: old.closed, 4)
    check("15. token --new 换了新令牌：旧地址立刻失效、正在收听的旧连接被断开、新地址能听",
          new != tok and new in r.stdout and dropped and http_get(port, "/%s.mp3" % tok)[0] == 404
          and http_get(port, "/%s.mp3" % new)[0] == 200, r.stdout.strip()[-120:])
    old.going = False
    stop_side(p)


def t16_keep(cfg):
    """说得比念得快时只留最近两段；被丢弃、被打断的合成结果及时删掉。"""
    fake_set("play-secs", "2.5")
    s = Session()
    s.on(cfg, LISTEN_TARGET="here")
    n = len(fake_log("afplay.log"))
    s.say("第一段。")
    wait_for(lambda: len(fake_log("afplay.log")) > n, 6)
    texts = ["第%d段%s。" % (i, "长" * (2 * i)) for i in range(2, 7)]
    s.say_many(texts)
    time.sleep(1.2)
    mid = tts_files()
    wait_for(lambda: len(fake_log("afplay.log")) >= n + 3 and not alive(read_pid(FAKE_DIR + "/afplay.pid")), 15)
    time.sleep(0.5)
    sizes = [int(x.split()[2]) for x in fake_log("afplay.log")[n:]]
    want = [len(tone(t)) for t in texts[-2:]]
    check("20. 说得比念得快时只留最近两段", len(sizes) == 3 and sizes[1:] == want,
          "实际播放 %s，最近两段应为 %s" % (sizes, want))
    check("16. 被积压规则丢弃的合成结果及时删掉（运行中临时目录不只增不减）", mid <= 3, "当时有 %d 个文件" % mid)
    s.say_many(["第%d条长回复，" % i + "这是一句用来打断的话，" * 12 for i in range(3)])
    time.sleep(1.5)
    s.user("停。")
    time.sleep(1.5)
    left = tts_files()
    check("16. 被打断丢弃的合成结果及时删掉", left == 0, "还剩 %d 个文件" % left)
    fake_set("play-secs", "0.3")
    s.off()


def t18_stale():
    p, rt, cfg, env = side_radio("stale", free_port(), stale=True, idle="2")
    exited = wait_for(lambda: p.poll() is not None, 12)
    left = glob.glob(rt + "/*-*-*-*-*.pid")
    check("18. 只剩陈旧的进程号文件时，电台照样空闲退出，并清掉陈旧文件", exited and not left,
          "电台%s，残留 %d 个" % ("已退出" if exited else "还在", len(left)))
    stop_side(p)



def t04b_slots():
    """名额：读完请求头有从连上算起的总期限；同一来源地址的未验证连接最多 2 个；路径不对立刻 404 并关闭。"""
    port = free_port()
    p, rt, cfg, env = side_radio("slots", port)
    tok = open(rt + "/token").read().strip() if os.path.exists(rt + "/token") else ""
    s, t0, closed_at = socket.create_connection(("127.0.0.1", port)), time.time(), None
    for i in range(10):     # 每秒补一个字节，一共 10 秒
        try:
            s.send(b"GET /x"[i:i + 1] or b"x")
            if select.select([s], [], [], 1.0)[0] and not s.recv(100):
                closed_at = time.time() - t0
                break
        except OSError:
            closed_at = time.time() - t0
            break
    s.close()
    check("R2-1. 慢慢滴请求头的连接，从连上算起约 5 秒就被断开（按墙钟算的总期限）",
          closed_at is not None and closed_at < 7, "断开于 %s 秒" % (None if closed_at is None else round(closed_at, 1)))
    time.sleep(0.5)     # 等电台那边把刚才那条连接的名额还回去
    socks = [socket.create_connection(("127.0.0.1", port)) for _ in range(5)]
    time.sleep(0.5)
    closed = 0
    for x in socks:
        x.setblocking(False)
        try:
            closed += x.recv(1) == b""
        except BlockingIOError:
            pass
        except OSError:
            closed += 1
        x.close()
    check("R2-1. 同一来源地址的未验证连接最多占 2 个，多出来的立刻关掉", closed == 3, "被关掉 %d 个" % closed)
    s = socket.create_connection(("127.0.0.1", port))
    s.settimeout(3)
    s.sendall(b"GET /wrong HTTP/1.1\r\nHost: x\r\n\r\n")
    data, ended = b"", False
    try:
        while True:
            b = s.recv(4096)
            if not b:
                ended = True
                break
            data += b
    except OSError:
        pass
    s.close()
    check("R2-1. 路径不对的请求立刻回 404 并关闭连接", ended and b" 404 " in data.split(b"\r\n")[0], data[:40])
    stop_side(p)


def t09_plain():
    cfg = write_cfg("plain-http", openai_cfg(base_url="http://0.0.0.0:%d/v1" % free_port()))   # 没人在听，不出网
    out1 = cli("doctor", cfg=cfg)
    s = Session()
    out2 = s.on(cfg)
    s.off()
    check("R2-10. doctor 输出里确实给出明文地址提醒", "明文" in out1, out1.splitlines()[-3:])
    check("R2-10. /listen 开启时输出里确实给出明文地址提醒", "明文" in out2, out2)


def t19_radio_gone():
    """手机电台念到一半没了：交给它的那段放弃、释放排队锁、记日志并提示，电台被重新拉起。"""
    port = free_port()
    p, rt, cfg, env = side_radio("gone", port)
    tok = open(rt + "/token").read().strip()
    ear = Listener(port, tok)
    ear.start()
    wait_for(lambda: ear.bytes > 0, 3)
    a, b = Session(rt), Session(rt)
    a.on(cfg, LISTEN_TARGET="phone")
    b.on(cfg, LISTEN_TARGET="here")
    a.say("甲会话的长回复，正在手机上念。" * 20)
    wait_for(lambda: spool_files(rt), 6)
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    n = len(fake_log("afplay.log"))
    b.say("乙会话在这台电脑上说的话。")
    played = wait_for(lambda: len(fake_log("afplay.log")) > n, 10)
    log = log_text(rt)
    check("R2-3. 电台念到一半没了，别的会话不会被一直占着排队锁，照样能念", played)
    check("R2-3. 电台没了的那个会话记了日志", "手机电台不在了" in log, log.splitlines()[-3:])
    back = wait_for(lambda: read_pid(rt + "/phone.pid") not in (0, p.pid) and ours(read_pid(rt + "/phone.pid")), 5)
    PIDS.add(read_pid(rt + "/phone.pid"))
    check("R2-3. 电台被重新拉起", back)
    ear.going = False
    a.off()
    b.off()
    if ours(read_pid(rt + "/phone.pid")):
        os.kill(read_pid(rt + "/phone.pid"), signal.SIGTERM)


def t21_drip(cfg):
    """语音服务慢慢滴数据：合成有总期限，到点中止并按失败处理；合成线程不会被永久占死。"""
    s = Session()
    s.on(cfg, LISTEN_TARGET="here")
    MODE["openai"] = "drip"
    s.say_many(["滴答测试第%d段。" % i for i in range(4)])
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("doctor", cli_safe("doctor", cfg=cfg)))
    th.start()
    time.sleep(3)
    MODE["openai"] = "ok"
    cut = wait_for(lambda: "60 秒内没有完成合成" in log_text(), 75)
    n = len(fake_log("afplay.log"))
    s.say("服务恢复之后的这句话。")
    played = wait_for(lambda: len(fake_log("afplay.log")) > n, 10)
    th.join(40)
    out = res.get("doctor", "")
    check("R2-4. 语音服务慢慢滴数据：合成超过总期限就中止，并记进日志", cut)
    check("R2-4. doctor 遇到慢慢滴数据时，到期限如实报失败，不会一直卡住",
          "试合成：失败" in out and "没有完成合成" in out, out.splitlines()[-1:])
    check("R2-4. 合成线程不会被永久占死：服务恢复后新的回复照常念", played)
    s.off()


def t22_logflood():
    port = free_port()
    p, rt, cfg, env = side_radio("logflood", port)
    before = len(log_text(rt))
    for _ in range(100):        # 路径不对的请求，发完立刻用复位断开
        try:
            x = socket.create_connection(("127.0.0.1", port))
            x.sendall(b"GET /wrong HTTP/1.0\r\n\r\n")
            x.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            x.close()
        except OSError:
            pass
    time.sleep(1.5)
    text = log_text(rt)[before:] + open("%s/side-logflood.out" % TMP).read()
    check("R2-5. 无令牌的请求处理出错时，日志里不写异常栈、不出现绝对路径",
          "Traceback" not in text and ROOT not in text and "/Users/" not in text, text[-200:])
    check("R2-5. 这类出错只记一行并限频，日志不会被灌大", len(text) < 3000, "增长 %d 字节" % len(text))
    stop_side(p)


def t23_status_errors(cfg):
    home = TMP + "/home23"
    rt = home + "/rt"
    os.makedirs(rt + "/token")      # 令牌文件的位置被一个目录占了
    with open(home + "/rtfile", "w") as f:
        f.write("x")
    outs = [("令牌位置是目录时 status", cli("status", cfg=cfg, HOME=home, LISTEN_RT=rt)),
            ("令牌位置是目录时 token --new", cli("token", "--new", cfg=cfg, HOME=home, LISTEN_RT=rt)),
            ("运行目录的位置是个文件时 /listen", cli("toggle", str(uuid.uuid4()), "on", cfg=cfg, HOME=home,
                                                LISTEN_RT=home + "/rtfile"))]
    os.rmdir(rt + "/token")
    with open(rt + "/token", "w") as f:
        f.write("0" * 32)
    os.chmod(rt + "/token", 0)
    outs.append(("令牌文件读不了时 status", cli("status", cfg=cfg, HOME=home, LISTEN_RT=rt)))
    os.chmod(rt + "/token", 0o600)
    for name, out in outs:
        check("R2-7. %s：只给一句中文和异常类型，不出异常栈和绝对路径" % name,
              "Traceback" not in out and home not in out and "Error" in out and re.search(r"[\u4e00-\u9fff]", out),
              out.splitlines()[-1:])


def test_install():
    def home(prefix="listen-e2e-home-"):
        h = tempfile.mkdtemp(prefix=prefix)
        HOMES.append(h)
        return h

    def sh(name, h):
        r = subprocess.run(["bash", ROOT + "/" + name], capture_output=True, text=True, env=dict(BASE, HOME=h),
                           timeout=60)
        r.out = r.stdout + r.stderr
        return r

    same = lambda a, b: open(a, "rb").read() == open(b, "rb").read()
    h = home()
    r = sh("install.sh", h)
    conf = h + "/.claude/listen/config.json"
    ok = r.returncode == 0 and os.path.exists(conf) and same(ROOT + "/listen.py", h + "/.claude/listen/listen.py") \
        and same(ROOT + "/commands/listen.md", h + "/.claude/commands/listen.md") \
        and same(ROOT + "/config.example.json", conf) and os.stat(conf).st_mode & 0o777 == 0o600
    check("安装：文件装到位，配置由样例生成且权限 600", ok and "自检通过" in r.stdout, r.out[-300:])
    with open(conf, "a") as f:
        f.write(" ")
    mine = open(conf, "rb").read()
    r = sh("install.sh", h)
    check("安装：重复执行不覆盖已改过的配置", r.returncode == 0 and open(conf, "rb").read() == mine
          and "保留不动" in r.stdout, r.stdout)
    check("5. 重复执行时内容相同的文件直接跳过，不产生备份",
          "已是最新" in r.stdout and not glob.glob(h + "/.claude/*/*.bak-*"), r.stdout)
    r = sh("uninstall.sh", h)
    check("卸载：删掉安装的文件和运行目录，保留配置并提示",
          r.returncode == 0 and not os.path.exists(h + "/.claude/listen/listen.py")
          and not os.path.exists(h + "/.claude/commands/listen.md") and os.path.exists(conf)
          and "保留" in r.stdout, r.stdout)

    h = home()
    cmd = h + "/.claude/commands/listen.md"
    os.makedirs(h + "/.claude/commands")
    with open(cmd, "w") as f:
        f.write("MY OWN COMMAND\n")
    r = sh("install.sh", h)
    baks = glob.glob(cmd + ".bak-*")
    check("5. 目标已有不同内容：先备份成 .bak-时间戳 再覆盖，并打印提示",
          r.returncode == 0 and len(baks) == 1 and open(baks[0]).read() == "MY OWN COMMAND\n"
          and "备份" in r.stdout and same(ROOT + "/commands/listen.md", cmd), r.out[-300:])
    with open(cmd, "w") as f:
        f.write("MY OWN COMMAND\n")
    r = sh("uninstall.sh", h)
    check("5. 卸载时同名但不是本工具的命令文件保留不删",
          os.path.exists(cmd) and open(cmd).read() == "MY OWN COMMAND\n", r.out[-200:])

    h = home()
    os.makedirs(h + "/.claude/commands")
    victim = h + "/victim.txt"
    with open(victim, "w") as f:
        f.write("VICTIM\n")
    os.symlink(victim, h + "/.claude/commands/listen.md")
    r = sh("install.sh", h)
    check("5. 目标是符号链接：不写入、报错说明，被指向的文件不变、也没装一半",
          r.returncode != 0 and "符号链接" in r.out and open(victim).read() == "VICTIM\n"
          and not os.path.exists(h + "/.claude/listen/listen.py"), r.out[-200:])
    h = home()
    os.makedirs(h + "/.claude/listen")
    os.symlink(h + "/elsewhere.json", h + "/.claude/listen/config.json")
    r = sh("install.sh", h)
    check("5. 配置文件是悬空的符号链接：拒绝，不顺着链接在别处建文件",
          r.returncode != 0 and not os.path.exists(h + "/elsewhere.json"), r.out[-200:])
    r = sh("install.sh", "")
    check("5. HOME 为空：明确报错，什么都不装", r.returncode != 0 and "HOME" in r.out, r.out[-200:])
    h = home("listen e2e home ")
    r = sh("install.sh", h)
    check("5. HOME 路径里有空格也能正常安装",
          r.returncode == 0 and os.path.exists(h + "/.claude/listen/listen.py"), r.out[-200:])
    h = home()
    os.makedirs(h + "/.claude/commands")
    os.chmod(h + "/.claude/commands", 0o555)
    r = sh("install.sh", h)
    os.chmod(h + "/.claude/commands", 0o755)
    check("R2-6. 命令目录不可写：在写任何东西之前就停下，整体不动",
          r.returncode != 0 and "安装没有进行" in r.out and not os.path.exists(h + "/.claude/listen"), r.out[-200:])
    h = home()
    os.makedirs(h + "/.claude")
    with open(h + "/.claude/commands", "w") as f:
        f.write("not a dir\n")
    r = sh("install.sh", h)
    check("R2-6. 命令目录的位置是个普通文件：中文报错，整体不动（不留空目录）",
          r.returncode != 0 and "安装没有进行" in r.out and not os.path.exists(h + "/.claude/listen"), r.out[-200:])
    h = home()
    cmd = h + "/.claude/commands/listen.md"
    os.makedirs(h + "/.claude/commands")
    for text in ("FIRST OWN VERSION\n", "SECOND OWN VERSION\n"):    # 同一秒内装两次，中间用户文件又变了
        with open(cmd, "w") as f:
            f.write(text)
        r = subprocess.run(["bash", ROOT + "/install.sh"], capture_output=True, text=True, timeout=60,
                           env=dict(BASE, HOME=h, FAKE_DATE="20260101-000000"))
    baks = sorted(open(x).read() for x in glob.glob(cmd + ".bak-*"))
    check("R2-6. 同一秒内备份两次，两份备份都保留、不互相覆盖", baks == ["FIRST OWN VERSION\n", "SECOND OWN VERSION\n"], baks)
    r = sh("uninstall.sh", h)
    check("R2-6. 卸载结束时列出遗留的备份文件并说明", r.out.count("listen.md.bak-") >= 2 and "备份" in r.out,
          r.out[-300:])

    # 卸载时还有活着的播报进程和电台；另有一个命令行里碰巧含 listen.py 的别的程序
    h = home()
    sh("install.sh", h)
    rt = h + "/.claude/listen/run"
    with open(h + "/.claude/listen/config.json", "w") as f:
        json.dump(openai_cfg(port=free_port()), f)
    env = {k: v for k, v in BASE.items() if k not in ("LISTEN_RT", "LISTEN_CONFIG")}
    sid = str(uuid.uuid4())
    subprocess.run([PY, h + "/.claude/listen/listen.py", "toggle", sid, "on"], env=dict(env, HOME=h),
                   capture_output=True, text=True, timeout=30)
    wait_for(lambda: read_pid(rt + "/phone.pid"), 5)
    run_pid, phone_pid = read_pid("%s/%s.pid" % (rt, sid)), read_pid(rt + "/phone.pid")
    PIDS.update({run_pid, phone_pid})
    decoy = subprocess.Popen(["/bin/sh", "-c", "sleep 60; : listen.py-someone-elses-tool"])
    with open("%s/%s.pid" % (rt, uuid.uuid4()), "w") as f:
        f.write(str(decoy.pid))
    r = sh("uninstall.sh", h)
    stopped = wait_for(lambda: not ours(run_pid) and not ours(phone_pid), 5)
    check("17/20. 卸载时停掉还活着的播报进程和电台", run_pid and phone_pid and stopped, r.out[-200:])
    check("17. 卸载只停核对过命令行的本程序进程，不误杀命令行里碰巧含 listen.py 的别的程序", decoy.poll() is None)
    if decoy.poll() is None:
        os.kill(decoy.pid, signal.SIGTERM)
    decoy.wait()


ONLY = set(filter(None, os.environ.get("E2E_ONLY", "").split(",")))    # 只跑这几项（排查用），空表示全跑


def guarded(fn, *args):
    if ONLY and fn.__name__ not in ONLY:
        return None
    try:
        return fn(*args)
    except Exception as ex:     # 一项出错不连累后面的项：记成不通过，接着跑
        check("%s 跑完（没有中途出错）" % fn.__name__, False, "%s: %s" % (type(ex).__name__, ex))


def cleanup(servers):
    for rt in [RT] + glob.glob(TMP + "/rt-*") + [h + "/.claude/listen/run" for h in HOMES]:
        PIDS.update(read_pid(rt + "/" + x) for x in pidfiles(rt))
    for p in PIDS:
        if ours(p):
            os.kill(p, signal.SIGTERM)
    time.sleep(1)
    for p in PIDS:
        if ours(p):
            os.kill(p, signal.SIGKILL)
    for s in servers:
        s.shutdown()
    for line in fake_log("afplay.log"):     # 远程那条路在本机 /tmp 下建的临时目录（正常情况下早已删掉）
        d = os.path.dirname(line.split()[-1])
        if d.startswith("/tmp/listen-tts-"):
            shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(PROJ, ignore_errors=True)
    for h in HOMES:
        shutil.rmtree(h, ignore_errors=True)
    shutil.rmtree(TMP, ignore_errors=True)
    print("已清理：%s 与 ~/.claude/projects/%s" % (TMP, os.path.basename(PROJ)), flush=True)


FAKE_TOOLS = {
    # 假 tmux：只记下被怎么调用，list-clients 报一个假客户端
    "tmux": '#!/bin/sh\necho "$*" >> "$FAKE_TMUX_LOG"\n[ "$1" = list-clients ] && echo /dev/ttys-fake\nexit 0\n',
    # 假 afplay：记下进程号、音量、文件大小、文件与所在目录的权限、路径；按开关文件决定放多久、退出码是几
    "afplay": '#!/bin/sh\nf="$3"\n'
              'echo "$$ $2 $(wc -c < "$f" | tr -d " ") $(stat -f %Lp "$f") $(stat -f %Lp "$(dirname "$f")") $f"'
              ' >> "$FAKE_DIR/afplay.log"\n'
              'echo $$ > "$FAKE_DIR/afplay.pid"\n'
              "trap 'kill $! 2>/dev/null; exit 143' TERM\n"
              'sleep "$(cat "$FAKE_DIR/play-secs" 2>/dev/null || echo 0.3)" & wait $!\n'
              'exit "$(cat "$FAKE_DIR/afplay-exit" 2>/dev/null || echo 0)"\n',
    # 假 ssh：去掉 -o 选项，记下别名和远程命令；开关文件在时模拟连不上（255），否则在本机执行远程命令
    "ssh": '#!/bin/sh\nwhile [ "$1" = "-o" ]; do shift 2; done\nhost="$1"; shift\n'
           'echo "$host $*" >> "$FAKE_DIR/ssh.log"\n'
           '[ -f "$FAKE_DIR/ssh-fail" ] && exit 255\n'
           'exec "$(cat "$FAKE_DIR/ssh-shell" 2>/dev/null || echo /bin/sh)" -c "$*"\n',
    # 假 date：设了 FAKE_DATE 时，安装脚本取到的时间戳固定不变（用来确定地复现"同一秒装两次"）
    "date": '#!/bin/sh\nif [ -n "$FAKE_DATE" ] && [ "$1" = "+%Y%m%d-%H%M%S" ]; then echo "$FAKE_DATE"; exit 0; fi\n'
            'exec /bin/date "$@"\n',
}


def main():
    for tool in (FFMPEG, FFPROBE):
        if not os.access(tool, os.X_OK):
            sys.exit("需要 ffmpeg 和 ffprobe（例如 brew install ffmpeg）")
    os.makedirs(PROJ)
    os.makedirs(FAKEBIN)
    os.makedirs(FAKE_DIR)
    for name, body in FAKE_TOOLS.items():
        with open(FAKEBIN + "/" + name, "w") as f:
            f.write(body)
        os.chmod(FAKEBIN + "/" + name, 0o755)
    servers = []
    for port, handler in ((TTS_PORT, FakeTTS), (STEAL_PORT, Steal)):
        srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    print("临时目录 %s，电台端口 %d，假语音服务端口 %d" % (TMP, RADIO_PORT, TTS_PORT), flush=True)
    try:
        cfg_openai, cfg_volc = write_cfg("openai", openai_cfg()), write_cfg("volc", volc_cfg())
        cfg_nocue = write_cfg("openai-nocue", openai_cfg(cue=""))
        guarded(test_selfcheck)
        guarded(test_bad_configs)
        guarded(t03_slash)
        guarded(test_nudge_and_tip, cfg_openai)
        s1 = Session()
        guarded(test_openai_interrupt, cfg_openai, s1)
        s2 = guarded(test_volc_multisession, cfg_volc, s1)
        guarded(test_failures, cfg_openai, cfg_volc, s1, s2)
        guarded(test_survive, cfg_openai)
        guarded(test_format, s1)
        guarded(t01_redirect, cfg_openai, cfg_volc)
        guarded(t02_server_text, cfg_openai, cfg_volc)
        guarded(t10_short_key)
        guarded(t13_tilde, cfg_openai)
        guarded(t08_malformed, cfg_openai)
        guarded(t06_playback, cfg_nocue)
        guarded(t16_keep, cfg_nocue)
        guarded(t04_flood)
        guarded(t04b_slots)
        guarded(t09_plain)
        guarded(t19_radio_gone)
        guarded(t21_drip, cfg_nocue)
        guarded(t22_logflood)
        guarded(t23_status_errors, cfg_openai)
        guarded(t07_port_busy)
        guarded(t11_perms)
        guarded(t15_token)
        guarded(t18_stale)
        guarded(test_secrets, cfg_openai, cfg_volc)
        guarded(test_closing)
        guarded(test_no_leak)
        guarded(test_install)
    finally:
        cleanup(servers)
    check("收尾：假会话目录已删除", not os.path.exists(PROJ))
    print("\n%d 项通过，%d 项不通过" % (results.count(True), results.count(False)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
