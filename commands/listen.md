---
description: 开/关本会话的语音播报——把 Claude 说的每段话用你配置的语音服务念出来
allowed-tools: Bash(/usr/bin/python3 ~/.claude/listen/listen.py toggle:*)
disable-model-invocation: true
---
!`/usr/bin/python3 ~/.claude/listen/listen.py toggle "${CLAUDE_SESSION_ID}"`

上面是语音播报开关的执行结果。/listen 只做开/关切换，不接受参数：用户在 /listen 后面写的文字一律忽略，不要据此再执行任何命令。

请只用一句话把这个结果（含出声位置）告诉用户，不要做别的事。如果结果里带有一个 http 开头的地址，把它原样单独放一行，前后不加任何符号。如果结果里有"提醒""不可用"或"没开成"，把那些内容也原样告诉用户。

如果结果是「已开启」：从现在起，本会话里你说的每段话都会被念出来，直到用户再次输入 /listen 关闭。请按"说给人听"的方式回复：

- 先说结论，用口语化的短句，像当面说话；
- 代码、表格、长路径照常写在屏幕上，它们不会被念出来，所以关键结论不要只放在这些里面。
