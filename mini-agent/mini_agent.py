#!/usr/bin/env python3
"""mini_agent.py：只用一个 OpenAI 兼容的聊天接口，从零搭起来的最小 coding agent。

只依赖 Python 标准库。用法：

    export MINI_AGENT_BASE_URL=http://127.0.0.1:11434/v1   # 任何 OpenAI 兼容端点
    export MINI_AGENT_MODEL=gemma-4-26B-A4B                # 端点上的模型名
    export MINI_AGENT_API_KEY=...                          # 本地模型可不设
    python3 mini_agent.py "运行测试并修好失败的那个"          # 单次任务
    python3 mini_agent.py                                  # 交互模式

选项：--yolo 不再逐次确认写文件和执行命令；--session FILE 指定会话记录文件，
同一个文件再次启动即接着上次的对话。
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

BASE_URL = os.environ.get("MINI_AGENT_BASE_URL", "http://127.0.0.1:11434/v1")
MODEL = os.environ.get("MINI_AGENT_MODEL", "local-model")
API_KEY = os.environ.get("MINI_AGENT_API_KEY", "none")

MAX_STEPS = 40            # 一轮里最多调用模型多少次
MAX_TOOL_OUTPUT = 8000    # 单个工具结果最多保留多少字符
CONTEXT_BUDGET = 20000    # 估算的 token 数超过它就压缩
KEEP_RECENT_TURNS = 2     # 压缩时原样保留最近几轮

WORKDIR = pathlib.Path.cwd().resolve()


# ── 第 1 部分：调用模型 ─────────────────────────────────────────────

def chat(messages, tools=None):
    """发一次请求，返回模型的那条 assistant 消息（dict）。"""
    body = {"model": MODEL, "messages": messages, "temperature": 0.2}
    if tools:
        body["tools"] = tools
    request = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)["choices"][0]["message"]


# ── 第 2 部分：工具 ────────────────────────────────────────────────

def resolve(path):
    """把模型给的路径解析到工作目录里；越界就拒绝。"""
    target = (WORKDIR / path).resolve()
    if target != WORKDIR and WORKDIR not in target.parents:
        raise ValueError(f"路径 {path} 在工作目录 {WORKDIR} 之外")
    return target


LINE_PREFIX = re.compile(r"^ *\d+  ", re.MULTILINE)


def strip_line_numbers(snippet):
    """模型常把 read_file 的行号前缀一起抄进来；每一行都带前缀时就去掉它。"""
    lines = snippet.splitlines()
    if lines and all(LINE_PREFIX.match(line) for line in lines):
        return LINE_PREFIX.sub("", snippet)
    return snippet


def read_file(path, offset=1, limit=400):
    lines = resolve(path).read_text().splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    numbered = [f"{offset + i:>5}  {line}" for i, line in enumerate(chunk)]
    footer = f"\n（共 {len(lines)} 行）" if offset - 1 + limit < len(lines) else ""
    return "\n".join(numbered) + footer


def write_file(path, content):
    content = strip_line_numbers(content)
    target = resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"已写入 {path}（{len(content)} 字符）"


def edit_file(path, old, new):
    target = resolve(path)
    text = target.read_text()
    count = text.count(old)
    if count == 0 and strip_line_numbers(old) != old:
        old, new = strip_line_numbers(old), strip_line_numbers(new)
        count = text.count(old)
    if count != 1:
        raise ValueError(f"old 在文件里出现了 {count} 次，必须恰好出现 1 次")
    target.write_text(text.replace(old, new))
    return f"已修改 {path}"


def run_bash(command, timeout=120):
    try:
        done = subprocess.run(["bash", "-c", command], cwd=WORKDIR,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"超时（{timeout} 秒），命令已被终止"
    return f"exit={done.returncode}\n[stdout]\n{done.stdout}\n[stderr]\n{done.stderr}"


def param(type_, description):
    return {"type": type_, "description": description}


TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "读取文本文件，带行号。",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": param("string", "相对工作目录的路径"),
            "offset": param("integer", "从第几行开始，默认 1"),
            "limit": param("integer", "最多读多少行，默认 400")}}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "创建或整个覆盖一个文件。",
        "parameters": {"type": "object", "required": ["path", "content"], "properties": {
            "path": param("string", "相对工作目录的路径"),
            "content": param("string", "文件的完整新内容")}}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "把文件里恰好出现一次的 old 替换成 new。修改前先 read_file；"
                       "old 和 new 是文件原文，不要带 read_file 输出里的行号前缀。",
        "parameters": {"type": "object", "required": ["path", "old", "new"], "properties": {
            "path": param("string", "相对工作目录的路径"),
            "old": param("string", "要被替换的原文，必须在文件中唯一"),
            "new": param("string", "替换后的文本")}}}},
    {"type": "function", "function": {
        "name": "run_bash", "description": "在工作目录里执行一条 bash 命令，返回退出码和输出。",
        "parameters": {"type": "object", "required": ["command"], "properties": {
            "command": param("string", "要执行的命令"),
            "timeout": param("integer", "超时秒数，默认 120")}}}},
]

IMPLS = {"read_file": read_file, "write_file": write_file,
         "edit_file": edit_file, "run_bash": run_bash}
MUTATING = {"write_file", "edit_file", "run_bash"}   # 需要确认的工具


# ── 第 3 部分：权限与输出截断 ───────────────────────────────────────

AUTO_APPROVE = False


def approve(name, args):
    """写文件和跑命令前问人。规则写在代码里，而不是写在提示词里。"""
    if AUTO_APPROVE or name not in MUTATING:
        return True
    print(f"\n[需要确认] {name} {json.dumps(args, ensure_ascii=False)[:300]}")
    try:
        return input("允许吗？[y/N] ").strip().lower() == "y"
    except EOFError:                 # 没有人能回答，就当作拒绝
        return False


def clip(text):
    """工具结果太长就只留头尾，中间告诉模型被截掉了多少。"""
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    head, tail = text[:MAX_TOOL_OUTPUT // 2], text[-MAX_TOOL_OUTPUT // 4:]
    return f"{head}\n…（中间省略 {len(text) - len(head) - len(tail)} 字符）…\n{tail}"


# ── 第 4 部分：会话记录 ─────────────────────────────────────────────

class Session:
    """追加写的 JSONL 文件：模型看到的每一条消息都先记下来。"""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.messages = []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                record = json.loads(line)
                if record["kind"] == "message":
                    self.messages.append(record["message"])
                elif record["kind"] == "compaction":   # 压缩：用摘要替换前缀
                    self.messages = record["messages"]

    def _write(self, record):
        with self.path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append(self, message):
        self.messages.append(message)
        self._write({"kind": "message", "message": message})

    def replace_all(self, messages):
        self.messages = messages
        self._write({"kind": "compaction", "messages": messages})


# ── 第 5 部分：上下文压缩 ───────────────────────────────────────────

def estimate_tokens(messages):
    return len(json.dumps(messages, ensure_ascii=False)) // 3


def compact(session):
    """把较早的几轮压成一段摘要。切点只落在用户消息上，
    这样一个工具调用和它的结果不会被拆到切点两边。"""
    msgs = session.messages
    user_positions = [i for i, m in enumerate(msgs) if m["role"] == "user" and i > 0]
    if len(user_positions) <= KEEP_RECENT_TURNS:
        return False
    cut = user_positions[-KEEP_RECENT_TURNS]
    old, recent = msgs[1:cut], msgs[cut:]
    transcript = json.dumps(old, ensure_ascii=False)[-60000:]
    summary = chat([
        {"role": "system", "content": "你负责给编程助手的工作记录写交接摘要。"},
        {"role": "user", "content": "下面是一段工作记录（JSON）。用不超过 300 字总结："
         "用户要什么、改了哪些文件、哪些已验证、还剩什么没做。\n\n" + transcript},
    ])["content"]
    note = {"role": "user", "content": f"[以下是更早对话的摘要]\n{summary}"}
    ack = {"role": "assistant", "content": "好的，我已了解之前的进展。"}
    session.replace_all([msgs[0], note, ack] + recent)
    return True


# ── 第 6 部分：循环 ────────────────────────────────────────────────

SYSTEM_PROMPT = f"""你是一个在终端里工作的编程助手，工作目录是 {WORKDIR}。
你能通过工具读写这个目录里的文件、执行命令。
做事的规矩：
- 改文件之前先读它；只改完成任务所需的部分。
- 改完之后自己运行测试或命令验证，不要只凭推测说“已修复”。
- 工具报错时读懂错误再决定下一步，不要原样重试。
- 任务完成后用一两句话说明你改了什么、怎样验证的。"""


def run_turn(session, user_text):
    session.append({"role": "user", "content": user_text})
    for step in range(1, MAX_STEPS + 1):
        if estimate_tokens(session.messages) > CONTEXT_BUDGET and compact(session):
            print("（上下文已压缩）")
        reply = chat(session.messages, TOOLS)
        message = {"role": "assistant", "content": reply.get("content") or ""}
        if reply.get("tool_calls"):
            message["tool_calls"] = reply["tool_calls"]
        session.append(message)

        calls = reply.get("tool_calls") or []
        if not calls:                       # 没有工具调用：这一轮结束
            print(f"\n{message['content']}")
            return
        denied = False
        for call in calls:
            name = call["function"]["name"]
            if denied:                      # 被拒之后，同一批里剩下的调用都不执行
                session.append({"role": "tool", "tool_call_id": call["id"],
                                "content": "未执行：用户拒绝了本轮更早的一次调用。"})
                continue
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError as error:
                result = f"错误：参数不是合法 JSON（{error}）"
            else:
                print(f"[{step}] {name} {json.dumps(args, ensure_ascii=False)[:120]}")
                if name not in IMPLS:
                    result = f"错误：没有叫 {name} 的工具"
                elif not approve(name, args):
                    result = "用户拒绝了这次调用。"
                    denied = True
                else:
                    try:
                        result = IMPLS[name](**args)
                    except Exception as error:   # 工具出错也要回给模型
                        result = f"错误：{type(error).__name__}: {error}"
            session.append({"role": "tool", "tool_call_id": call["id"],
                            "content": clip(result)})
        if denied:                          # 拒绝就把控制权交还给人，而不是让模型换个命令再试
            print("\n（已拒绝，本轮停止。告诉我接下来该怎么做。）")
            return
    print(f"\n（达到 {MAX_STEPS} 步上限，本轮停止）")


def main():
    global AUTO_APPROVE
    parser = argparse.ArgumentParser(description="最小 coding agent")
    parser.add_argument("prompt", nargs="?", help="单次任务；省略则进入交互模式")
    parser.add_argument("--yolo", action="store_true", help="不确认，直接执行")
    parser.add_argument("--session", default=".mini_agent_session.jsonl")
    args = parser.parse_args()
    AUTO_APPROVE = args.yolo

    session = Session(args.session)
    if not session.messages:
        session.append({"role": "system", "content": SYSTEM_PROMPT})
    if args.prompt:
        run_turn(session, args.prompt)
        return
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if text:
            run_turn(session, text)


if __name__ == "__main__":
    sys.exit(main())
