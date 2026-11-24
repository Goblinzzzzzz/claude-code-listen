# claude-code-listen

**English:** `claude-code-listen` adds a `/listen` slash command to [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Once enabled, every reply Claude writes in that session is turned into speech with a text-to-speech service you configure (any OpenAI-compatible `/v1/audio/speech` endpoint, or Volcengine Doubao TTS) and played on your Mac's speakers, on a remote Mac you are SSH-ing from, or streamed to your phone as a never-ending MP3 "radio". macOS only; single Python file using the system `/usr/bin/python3` and the standard library. The documentation below is in Chinese.

---

## 这是什么

给 Claude Code 加一个斜杠命令 `/listen`。开启后，Claude 在这个会话里说的每段话都会被合成语音念出来——人可以离开屏幕，边听边干别的事；用户一开口，没念完的立刻停下，像聊天一样。

**一句话原理**：一个后台小程序盯着本会话的记录文件（`~/.claude/projects/*/<会话编号>.jsonl`），Claude 每写完一段话，就把它清洗成适合念的文字（代码块、表格、链接地址不念），切成小块，调你配置的语音服务合成 mp3 并播放；所属的 Claude 会话结束后它自己退出。

## 适用环境

- **macOS**（播放用系统自带的 `afplay`）+ **Claude Code**。
- 系统自带的 `/usr/bin/python3`（3.9 起即可，只用标准库，不装任何依赖）。如果提示没有 python3，先安装苹果命令行工具：`xcode-select --install`。
- 一个语音合成服务的账号：火山引擎豆包语音，或任何兼容 OpenAI 语音接口（`/v1/audio/speech`）的服务。
- 想用**手机收听**还需要：`ffmpeg`（例如 `brew install ffmpeg`）和手机上一个能播网络串流的播放器（例如 VLC）。
- 可选：`tmux`。用 tmux 时能更准确地判断"你此刻在哪台设备上打字"。

## 安装

下载或克隆本仓库，在仓库目录里执行：

```bash
./install.sh
```

它会：

- 把 `listen.py` 拷到 `~/.claude/listen/listen.py`，把 `commands/listen.md` 拷到 `~/.claude/commands/listen.md`；
- 如果 `~/.claude/listen/config.json` 还不存在，从 `config.example.json` 生成一份，权限设为 600（只有你自己能读）；已经存在就保留不动；
- 跑一遍自检。

可以重复执行，升级时再跑一遍即可：

- 内容没变的文件直接跳过；
- 目标位置已经有**内容不同**的同名文件（比如你自己写过一个 `/listen` 命令）时，先把它备份成 `<名字>.bak-<时间>-<进程号>` 并打印提示，再换上本工具的版本（同一秒里装两次也不会盖掉上一份备份）；
- 你填好的配置不会被覆盖；
- 这三个目标中任何一个是**符号链接**时，安装直接停下并说明原因，不会顺着链接去改别处的文件；
- 写任何东西之前，先检查两个目标目录（`~/.claude/listen`、`~/.claude/commands`）能建、能写，已有的同名文件能读；`HOME` 为空、目录不可写、目录的位置被一个普通文件占着等情况，都会用中文说明原因并整体不动，不会装一半。

装好后打开 `~/.claude/listen/config.json` 填上语音服务（见下一节），然后体检一次：

```bash
/usr/bin/python3 ~/.claude/listen/listen.py doctor
```

`doctor` 会逐项检查配置、依赖，并真的合成一句短话（不播放），告诉你成功还是失败、失败原因是什么。

## 配置语音服务

配置文件是 JSON，默认位置 `~/.claude/listen/config.json`。`provider` 选用哪一家：`volcengine` 或 `openai`。只需要填你选的那一节。

> 密钥也可以不写进文件，改用环境变量 `LISTEN_API_KEY` 提供（优先于配置文件里的 `api_key`）。
> 程序不会在任何输出或日志里显示密钥，体检时只报"已填写/未填写"。密钥短于 8 位时，体检和 `/listen` 会提醒"密钥过短，疑似填错"。

### 方式一：火山引擎豆包语音（`"provider": "volcengine"`）

使用火山引擎「豆包语音 · 单向流式语音合成 HTTP V3」接口。

| 配置项 | 填什么 |
|---|---|
| `endpoint` | 保持默认 `https://openspeech.bytedance.com/api/v3/tts/unidirectional` |
| `api_key` | **新版控制台**：控制台 → API Key 管理里的 API Key。填了它就只用它鉴权 |
| `app_id`、`access_key` | **旧版控制台**：分别填控制台里的 APP ID 和 Access Token。只有 `api_key` 留空时才会用这两项 |
| `resource_id` | 要和音色匹配：`seed-tts-2.0` 对应「豆包语音合成模型 2.0」的音色，`seed-tts-1.0` 对应 1.0 的音色，`seed-icl-2.0` 对应声音复刻 2.0 的音色 |
| `voice` | 音色 ID，在控制台的音色库里找，例如 2.0 音色 `zh_female_vv_uranus_bigtts` |
| `speed` | 倍速，1.0 是原速，2.0 是两倍速，0.5 是半速（会换算成接口的 `speech_rate`，超出范围会被夹到 0.5~2 倍） |
| `extra_headers` | 需要额外带的请求头，一般留空 `{}` |

`resource_id` 和音色不匹配、或者账号没开通这个音色时，服务会返回错误码（例如 `code=45000000`）。`doctor` 和日志里只显示这个数字错误码和程序自己写的一句中文说明（未知的错误码就写"未知错误码"），**不显示服务端返回的错误原文**：那段原文里可能夹着打了码的密钥或回显的请求文字。

> **说明**：火山引擎接入按官方文档实现（[单向流式语音合成 HTTP 文档](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-http)），发布时作者未对真实服务做联调，只用按文档形状搭建的本地假服务测试过。如果你在真实服务上遇到问题，欢迎提 issue 并附上 `doctor` 的输出：它不含密钥，家目录下的路径也显示成 `~/…`，但贴之前请自己再看一眼。

### 方式二：兼容 OpenAI 语音接口的服务（`"provider": "openai"`）

适用于 OpenAI 本身，以及任何提供 `POST /v1/audio/speech` 接口的服务（各类网关、自建服务）。

| 配置项 | 填什么 |
|---|---|
| `base_url` | 服务地址，例如 `https://api.openai.com`。写成 `https://example.com/v1` 也可以，不会重复加 `/v1` |
| `api_key` | 服务的密钥，以 `Authorization: Bearer <密钥>` 发送 |
| `model` | 语音模型，例如 `gpt-4o-mini-tts` 或 `tts-1` |
| `voice` | 音色，例如 `alloy`；其它服务以它们的文档为准 |
| `speed` | 倍速，1.0 是原速 |
| `extra_headers` | 有的网关要求额外的请求头（例如自定义 `User-Agent` 或分组标识），写在这里，会覆盖同名的默认请求头 |

请求体只带 `model`、`voice`、`input`、`response_format`（固定 `mp3`）、`speed` 五个字段。有的服务对无效音色会返回成功状态但空正文，程序会把这种情况当作失败处理，而不是"哑了还当成功"。

两种服务都**不跟随跳转**：语音服务如果回 301/302 之类的跳转，程序当作失败（"语音服务返回了跳转，已拒绝跟随"），不会把带着密钥的请求转发到另一个地址。

### 其它配置项

| 配置项 | 作用 |
|---|---|
| `volume` | 播放音量 0~1（在系统音量之上再调一道），默认 1.0 |
| `switch_cue` | 多个会话轮流说话时，换会话前先念的提示语，默认 `另一个对话：`；留空 `""` 则不念 |
| `phone.port` | 手机电台的端口，默认 18945 |
| `phone.advertise_host` | 收听地址里显示的主机名或 IP。留空时自动探测：先试 `tailscale ip -4`，再取本机默认网卡的地址 |
| `phone.max_listeners` | 手机电台同时在线的连接数上限，默认 8；超出的连接直接关掉 |
| `devices` | 让远程电脑出声用，见下文 |
| `remotes` | 同上，按来源 IP 认设备 |

## 日常用法

在 Claude Code 里输入 `/listen`：开着就关，关着就开。它不接受参数，后面写什么都会被忽略。开启后 Claude 会告诉你声音从哪里出来。每个会话分别开关。

几个命令行工具（都不出声，`say` 除外）：

```bash
/usr/bin/python3 ~/.claude/listen/listen.py status       # 哪些会话开着、手机电台在不在、几台设备在听、收听地址
/usr/bin/python3 ~/.claude/listen/listen.py doctor       # 体检
/usr/bin/python3 ~/.claude/listen/listen.py token --new  # 更换手机收听地址里的令牌
/usr/bin/python3 ~/.claude/listen/listen.py say 你好      # 试音
```

`listen.py toggle <会话编号> on|off` 可以在命令行里明确开或关某个会话；只认 `on` 和 `off`，写别的会报错、不动开关。

## 声音从哪里出来（自动选择）

每念一小块之前，程序都会重新判断一次"你此刻在哪台设备上对话"：

1. 用 tmux 时，取**最近在打字的那个 tmux 客户端**的终端；不用 tmux 时，取 Claude 自己所在的终端。
2. 再用 `who` 查这个终端是从哪里连进来的：
   - **本机终端** → 这台电脑的扬声器（`afplay`）；
   - **通过 SSH 连进来、而且是登记过的电脑** → 经 `ssh <别名>` 把音频推到那台电脑上用 `afplay` 播放；
   - **其它远程设备**（比如手机上的 SSH 客户端）→ 按"手机"处理：手机播放器正在收听电台就送过去；没在收听就不出声，并在 tmux 底部闪一行提示（30 秒内最多一次）。

播放本身失败时（远程电脑连不上、`afplay` 出错）也不会悄无声息：日志里会记一笔，并在 tmux 底部闪一行提示，说明是哪台设备（30 秒内最多一次）。

需要临时指定时，可以设环境变量 `LISTEN_TARGET`：`here`（本机）、`phone`（手机电台）、某个 ssh 别名，或空串（不出声）。

## 用手机收听

手机上的 SSH 客户端没法直接放声音，所以这台电脑会开一个"手机电台"：一条永不结束的 mp3 流，有话说时送语音，没话说时送静音（持续播放，手机系统才不会在后台把播放器掐掉）。

1. 确保已安装 `ffmpeg`，手机和这台电脑在**同一个局域网**，或在**同一个虚拟内网**里（例如都装了 Tailscale 并登录同一账号）。
2. 在 Claude Code 里先 `/listen`。没有设备在收听时，结果里会给出收听地址，形如 `http://100.64.0.10:18945/<32 位随机令牌>.mp3`（也可以随时用 `status` 查看）。
3. 在手机的播放器（例如 VLC 的"网络串流"）里打开这个地址，保持播放。
4. 回到对话。之后 Claude 说的话就会从手机里出来。

- 第一次开启时，macOS 防火墙可能会询问是否允许 python3 接受传入连接，需要允许。
- 电台起不来时（端口被别的程序占着、没装 ffmpeg 等），`/listen` 会直接说"手机收听不可用"和原因，不会给出一个打不开的地址；这台电脑和远程电脑出声不受影响。
- 收听地址里的令牌生成一次后固定不变，可以在播放器里收藏。地址泄露了就运行 `listen.py token --new`：旧地址立刻失效，正在用旧地址收听的设备会在一秒内被断开，新地址会打印出来。
- 电台同时在线的连接数有上限（`phone.max_listeners`，默认 8）；从连上算起 5 秒内没发完请求的连接会被断开（慢慢地一点点发也一样）；同一个地址最多同时有 2 个还没通过令牌校验的连接；路径不对的请求立刻回 404 并断开。即便这样，**同一网络里的人即使不知道令牌，也可能通过不停地占连接让新设备暂时连不上**，所以手机电台只应在可信网络里用。
- 电台中途没了（崩溃或被关掉）时，交给它的那一段会被放弃，日志里记一笔、tmux 底部提示一下，播报进程随即把电台重新拉起，手机重连即可；别的会话不会因此被卡住。
- 电台在没有会话开着播报、也没人收听十分钟后自动退出，下次 `/listen` 会重新拉起。

## 让远程电脑出声

场景：你在笔记本上 `ssh` 连到跑 Claude 的这台电脑，希望声音从笔记本上出来。需要三步：

**① 在笔记本上**：在它的 `~/.ssh/config` 里、连到 Claude 这台电脑的那个 Host 下面，加一行自报身份（名字只能用小写字母、数字和 `-`）：

```
Host claude-box
  HostName 192.0.2.10
  SetEnv LC_LISTEN_DEVICE=laptop
```

macOS 的 sshd 默认接受 `LC_*` 变量（`/etc/ssh/sshd_config.d/100-macos.conf` 里有 `AcceptEnv LANG LC_*`），一般不用改。

**② 在跑 Claude 的这台电脑上**：在 `~/.zshenv` 末尾加一小段，把「哪个终端是哪台设备」记下来：

```zsh
# claude-code-listen：通过 SSH 登录进来时，记下"这个终端是哪台设备"（对方用 LC_LISTEN_DEVICE 自报）。
# 没自报的也写成空，免得这个终端名沿用上一次登录留下的记录。tmux 里面的 shell 不算登录现场，跳过。
# umask 077：这里建出的运行目录只有你自己能读（里面以后还会放待播的音频和收听令牌）。
if [[ -n "$SSH_TTY" && -z "$TMUX" ]]; then
  (umask 077 && mkdir -p ~/.claude/listen/run/tty &&
    print -r -- "${LC_LISTEN_DEVICE//[^a-z0-9-]/}" >| ~/.claude/listen/run/tty/${SSH_TTY:t}) 2>/dev/null
fi
```

（如果你用环境变量 `LISTEN_RT` 改过运行目录，把这段里的 `~/.claude/listen/run` 换成同一个位置。）

**③ 还是在这台电脑上**：在配置文件的 `devices` 里登记「设备名 → 本机 ssh 配置里连回笔记本的别名」，并确认能免密登录过去：

```json
"devices": { "laptop": "my-laptop" }
```

```bash
ssh -o BatchMode=yes my-laptop true && echo 可以免密登录
```

笔记本那边要打开「系统设置 → 通用 → 共享 → 远程登录」，并把这台电脑的公钥加进它的 `~/.ssh/authorized_keys`。ssh 别名只能用字母、数字和 `. _ -`，且不能以 `-` 开头（它会进命令行，程序会校验）。

推到那台电脑上的命令统一用 `sh -c` 执行，要求它有 `/bin/sh`（macOS 自带）；它的登录 shell 是 zsh、bash、fish、tcsh 都可以。

音频会临时落在笔记本的 `/tmp` 下：每段放在一个用 `mktemp` 随机生成、只有你自己能读的目录里（`/tmp/listen-tts-…`），放完就删；被打断时只清理本次播报自己建的那些。

如果笔记本的来源 IP 是固定的，也可以不做 ①②，直接按 IP 登记：`"remotes": { "100.64.0.10": "my-laptop" }`。`devices` 和 `remotes` 都为空时，所有远程连进来的设备都按"手机"处理。

## 多个会话同时开着

- 每个开了播报的会话都会出声，但**同一时刻只有一个在念**：先拿到的会话念完手头这一批，再轮到下一个，不会叠在一起。
- 换了说话的会话时，先念一句提示语（`switch_cue`，默认"另一个对话："），让你听得出换了对话。
- 你在某个会话里发言，只打断**这个会话**没念完的内容，不影响别的会话。
- Claude 说得比念得快时，只保留最近两段，过时的步骤播报会被丢掉（已经合成好的临时音频也随即删掉）。

## 隐私与安全

- **哪些文字会发往你配置的语音服务**：Claude 的回复、工具调用前的旁白（工具执行前说的那句"我去查一下"之类）、换会话时的提示语 `switch_cue`、`doctor` 的试合成句子、`say` 的文字。不想让某些内容出会话，就不要在那个会话里开播报。
- **密钥**只用来发请求：不会出现在任何命令输出、`doctor` 结果或日志里（只显示"已填写/未填写"）。HTTP 出错时只记状态码；火山引擎再带上数字错误码和程序自己写的中文说明；服务端返回的任何文字都不显示、不记日志（有的服务会在错误正文里回显打了码的密钥或请求文本）。作为最后一道保险，读到过的密钥在写日志和输出前还会被抹掉（短于 8 位的不抹，以免把正常文字搅乱）。配置文件建议保持 600 权限，权限过宽时 `/listen` 会提醒。
- **传输**：地址可以填 `http://`，但不建议——密钥和要念的文字会不加密地经过网络，`doctor` 和 `/listen` 对不是本机的 `http://` 地址会给出提醒。请求会遵循系统和环境变量里的代理设置（`http_proxy`、`https_proxy` 等）；明文 `http://` 请求经过代理时，代理能看到全部内容。
- **手机电台监听这台电脑的所有网卡**，地址里有一段 128 位的随机令牌，但**没有加密，也没有登录**。只应在可信网络（家里的局域网、你自己的虚拟内网）里使用，不要把这个端口暴露到公网。令牌会留在播放器的历史记录里；怀疑泄露就用 `listen.py token --new` 换一个（见上文）。电台的响应头里不带 Python 版本等信息。
- **运行目录** `~/.claude/listen/run/` 及其子目录权限为 700，里面临时存放待播的音频、收听令牌和日志。
- **日志**（`~/.claude/listen/run/listen.log`）记录：开始/停止播报（含会话编号和停止原因）、收听设备的 IP 地址、合成或播放失败的原因（只是错误类型、状态码这类，程序自己写的）、处理不了的记录行（只记出错的位置，不记那一行的内容）、电台处理请求时出的错（来源地址和错误类型，十秒最多一条）。不记录回复内容，也不写异常栈：出错只记一行错误类型和出错位置（文件名和行号）。日志超过 1MB 时，旧的改名为 `listen.log.1`（只留一份）。

## 已知限制

- 只支持 macOS。
- 依赖 Claude Code 会话记录文件的格式，以及记录里一个未公开的标记（用来识别"工具调用前的旁白"）；Claude Code 改版后可能需要跟着调整，表现为"步骤播报变少"，最终回复不受影响。看不懂、嵌套过深或超过 5MB 的记录行会被跳过并记进日志；只有记录文件本身不见了这类情况才会让播报进程退出，退出前会把原因写进日志。
- 文本清洗和断句主要针对中文；英文也能念，但断句较粗。
- 语音有合成延迟，长回复会切成小块边合成边播。
- 手机电台开播时按 24kHz 单声道送静音，之后跟随最近一段语音的格式；如果你的语音服务输出别的格式，刚连上后的第一段语音处会切换一次格式。
- 同一网络里的人即使不知道令牌，也可能通过不停地占连接，让新设备一直连不上（已在收听的设备不受影响）；所以手机电台只应在可信网络里用。
- 每个后台进程只在启动时读一次配置：改了配置要 `/listen` 关掉再打开才生效。
- Claude 进程退出后，播报进程不会立刻退出：同一个会话可能被重新拉起（进程号会变），所以它会等到会话记录文件 2 小时没有更新才退出。想立刻停，再输一次 `/listen` 关掉即可。
- 远程电脑出声需要这台电脑能免密 `ssh` 到那台电脑，且那台电脑也是 macOS（用 `afplay` 播放）。
- 火山引擎接入未对真实服务做联调（见上文）。

## 排错

1. 先跑体检：`/usr/bin/python3 ~/.claude/listen/listen.py doctor`。
2. 看日志：`~/.claude/listen/run/listen.log`。合成失败会记成"合成失败：…"并响一声提示音；播放失败会记成"播放失败：…"并在 tmux 底部提示；播报进程因为意外原因退出时，也会先把原因记下来。
3. 常见情况：
   - `语音服务返回 HTTP 401` / `403`：密钥不对或没有权限；
   - `语音服务返回 HTTP 404`：`base_url` 或 `model` 不对；
   - `语音服务返回了跳转（状态码 …），已拒绝跟随`：地址填得不对（比如少了 `https`、域名换过），改成跳转之后的正确地址；
   - `语音服务返回的不是 mp3 音频（0 字节）`：多半是音色名不对；
   - `语音服务 60 秒内没有完成合成，已放弃`：语音服务或中间的网关、代理太慢或卡住了；
   - `手机电台不在了…`：电台崩溃或被关掉了，播报进程会自动把它重新拉起，手机重连即可；
   - `出了意料之外的错（某某Error）` / `读写 ~/… 时出错（某某Error）`：程序没预料到的情况，只给出错误类型；可以带着 `doctor` 的输出提 issue；
   - `火山引擎返回错误 code=…（…）`：括号里是程序给的常见原因，最常见的是 `resource_id` 和音色不匹配、或音色没开通；
   - `连不上远程电脑 …（ssh 失败）`：那台电脑合盖、断网或免密登录失效，先用 `ssh -o BatchMode=yes <别名> true` 试一下；
   - 手机连不上电台：确认手机与电脑在同一网络、防火墙已允许、端口没被占用（`status` 里能看到电台在不在、上次没开成的原因、收听地址）；
   - 本机没声音：检查系统音量和配置里的 `volume`；用 `say` 子命令试音。
4. 用 `status` 看哪些会话开着、电台在不在、有几台设备在听。

常用环境变量：

| 变量 | 作用 |
|---|---|
| `LISTEN_CONFIG` | 配置文件位置，默认 `~/.claude/listen/config.json` |
| `LISTEN_RT` | 运行目录，默认 `~/.claude/listen/run` |
| `LISTEN_API_KEY` | 密钥，优先于配置文件 |
| `LISTEN_TARGET` | 强制出声位置：`here` / `phone` / ssh 别名 / 空串（不出声） |
| `LISTEN_VOLUME` | 覆盖配置里的音量 |
| `LISTEN_IDLE` | Claude 进程不在后，记录文件多少秒没更新就退出，默认 7200 |
| `LISTEN_PHONE_IDLE` | 手机电台空闲多少秒后退出，默认 600 |

## 测试

```bash
/usr/bin/python3 listen.py check          # 自检：不联网、不出声
/usr/bin/python3 tests/test_e2e.py        # 端到端测试，约 4 分钟，需要 ffmpeg
```

端到端测试会起一个本地假语音服务（同时模拟两种接口），用临时的运行目录、配置和 18950 以上的端口，不读取任何真实密钥与配置，也不会出声：走本机和远程电脑的几项用假的 `afplay`、`ssh`、`tmux` 代替；结束后清理自己造的所有东西。

## 卸载

```bash
./uninstall.sh
```

它会停掉播报进程和手机电台（只停运行目录里记着、并且核对过确实是本工具的进程），删除 `~/.claude/listen/listen.py`、`~/.claude/commands/listen.md` 和运行目录；同名但不是本工具装的文件会保留并提示；安装时为你原来的同名文件做的备份（`*.bak-*`）不会被删，卸载结束时会列出来。配置文件（里面有你的密钥）默认保留，按提示手动删除即可。如果你在 `~/.zshenv` 里加过记录设备的那段，也请手动删掉。

## 许可证

MIT，见 [LICENSE](LICENSE)。
