#!/usr/bin/env python3
"""reliable_agent.py：在 mini_agent.py 的基础上，补上崩溃恢复、幂等键、取消和提交闸门，
并且模型接口可以换成脚本化的假模型来做确定性测试。

只依赖 Python 标准库。用法与 mini_agent.py 相同：

    export MINI_AGENT_BASE_URL=http://127.0.0.1:11434/v1
    export MINI_AGENT_MODEL=gemma-4-26B-A4B
    python3 reliable_agent.py "修好失败的测试，确认通过后提交"

另外的环境变量：
    RA_VERIFY_CMD      验证命令，默认 python3 -m pytest -q
    RA_PAY_LEDGER      演示用付款服务的账本文件
    RA_PAY_IDEMPOTENT  付款服务是否支持幂等键，默认 1
    RA_CRASH_AT        故障注入：在这些位置让进程立刻退出（逗号分隔）
    RA_SANDBOX         bwrap（默认）：run_bash 和验证命令里 .git 只读；off：不隔离

同一个 --session 文件再次启动时，如果上一轮没有结束，先补齐缺失的工具结果，再接着跑。
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

MAX_STEPS = 40            # 一轮里最多调用模型多少次
MAX_TOOL_OUTPUT = 8000    # 单个工具结果最多保留多少字符
CONTEXT_BUDGET = 20000    # 估算的 token 数超过它就压缩
KEEP_RECENT_TURNS = 2     # 压缩时原样保留最近几轮
RETRY_DELAYS = [1, 2, 4]  # 模型调用遇到暂时性错误时，第几次重试前等多少秒
VERIFY_CMD = os.environ.get("RA_VERIFY_CMD", "python3 -m pytest -q")
SANDBOX = os.environ.get("RA_SANDBOX", "bwrap")   # bwrap：shell 里 .git 只读；off：不隔离
CRASH_AT = set(filter(None, os.environ.get("RA_CRASH_AT", "").split(",")))


def crash_point(name):
    """故障注入：RA_CRASH_AT 里列出的位置上，进程立刻退出，不做任何清理。"""
    if name in CRASH_AT:
        os._exit(137)


# ── 第 1 部分：模型接口 ─────────────────────────────────────────────

class LLMError(Exception):
    def __init__(self, message, transient):
        super().__init__(message)
        self.transient = transient        # 暂时性错误（超时、限流、5xx）才值得重试


class OpenAIChat:
    """任何 OpenAI 兼容的 /chat/completions 端点。"""

    def __init__(self, base_url, model, api_key):
        self.base_url, self.model, self.api_key = base_url, model, api_key

    def chat(self, messages, tools=None):
        body = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            body["tools"] = tools
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.load(response)["choices"][0]["message"]
        except urllib.error.HTTPError as error:
            transient = error.code in (408, 429) or error.code >= 500
            raise LLMError(f"HTTP {error.code}", transient) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise LLMError(str(error), transient=True) from error


# ── 第 2 部分：日志 ────────────────────────────────────────────────

class Journal:
    """追加写的 JSONL。message / compaction 两种记录决定模型看到什么；
    其余记录（审批、工具开始、验证、提交）是 harness 自己的事实，压缩碰不到它们。"""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.records, self.messages = [], []
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        valid = 0
        for line in data.splitlines(keepends=True):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if data[valid + len(line):].strip():
                    raise ValueError(f"{self.path} 第 {len(self.records) + 1} 条记录损坏")
                # 最后一行写到一半就崩溃了：截掉这半行，之后照常追加
                with self.path.open("r+b") as f:
                    f.truncate(valid)
                break
            valid += len(line)
            self._apply(record)

    def _apply(self, record):
        self.records.append(record)
        if record["kind"] == "message":
            self.messages.append(record["message"])
        elif record["kind"] == "compaction":
            self.messages = list(record["messages"])

    def log(self, kind, **data):
        record = {"kind": kind, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())          # 返回之前已经落盘
        self._apply(record)
        return record

    def append(self, message):
        self.log("message", message=message)

    def find(self, kind, **match):
        return [r for r in self.records
                if r["kind"] == kind and all(r.get(k) == v for k, v in match.items())]

    def turn_open(self):
        for record in reversed(self.records):
            if record["kind"] == "turn_end":
                return False
            if record["kind"] == "turn_start":
                return True
        return False

    def steps_in_turn(self):
        start = max(i for i, r in enumerate(self.records) if r["kind"] == "turn_start")
        return sum(1 for r in self.records[start:]
                   if r["kind"] == "message" and r["message"]["role"] == "assistant")


# ── 第 3 部分：工具 ────────────────────────────────────────────────

class Cancelled(Exception):
    pass


class ExecCtx:
    """一次工具调用的上下文：工作目录、幂等键、取消信号、日志。"""

    def __init__(self, workdir, key, cancel, journal, call_id):
        self.workdir, self.key, self.cancel = workdir, key, cancel
        self.journal, self.call_id = journal, call_id


class Tool:
    def __init__(self, name, description, params, impl, *, sensitive,
                 read_only=False, idempotent=False):
        self.name, self.impl = name, impl
        self.sensitive = sensitive        # 执行前要经过审批
        self.read_only = read_only        # 不改变任何东西，结果未知时可以直接重做
        self.idempotent = idempotent      # 带同一个幂等键重做，外部世界只发生一次
        required = [p for p, (_, _, optional) in params.items() if not optional]
        self.schema = {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "required": required, "properties": {
                p: {"type": t, "description": d} for p, (t, d, _) in params.items()}}}}


def resolve(workdir, path):
    target = (workdir / path).resolve()
    if target != workdir and workdir not in target.parents:
        raise ValueError(f"路径 {path} 在工作目录 {workdir} 之外")
    return target


LINE_PREFIX = re.compile(r"^ *\d+  ", re.MULTILINE)


def strip_line_numbers(snippet):
    """模型常把 read_file 的行号前缀一起抄进来；每一行都带前缀时就去掉它。"""
    lines = snippet.splitlines()
    if lines and all(LINE_PREFIX.match(line) for line in lines):
        return LINE_PREFIX.sub("", snippet)
    return snippet


def read_file(args, ctx):
    offset, limit = int(args.get("offset", 1)), int(args.get("limit", 400))
    lines = resolve(ctx.workdir, args["path"]).read_text().splitlines()
    chunk = lines[offset - 1: offset - 1 + limit]
    numbered = [f"{offset + i:>5}  {line}" for i, line in enumerate(chunk)]
    footer = f"\n（共 {len(lines)} 行）" if offset - 1 + limit < len(lines) else ""
    return "\n".join(numbered) + footer


def write_file(args, ctx):
    content = strip_line_numbers(args["content"])
    target = resolve(ctx.workdir, args["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"已写入 {args['path']}（{len(content)} 字符）"


def edit_file(args, ctx):
    target = resolve(ctx.workdir, args["path"])
    old, new = args["old"], args["new"]
    text = target.read_text()
    count = text.count(old)
    if count == 0 and strip_line_numbers(old) != old:
        old, new = strip_line_numbers(old), strip_line_numbers(new)
        count = text.count(old)
    if count != 1:
        raise ValueError(f"old 在文件里出现了 {count} 次，必须恰好出现 1 次")
    target.write_text(text.replace(old, new))
    return f"已修改 {args['path']}"


def kill_group(proc):
    """先 SIGTERM 整个进程组，两秒后还在就 SIGKILL。"""
    for sig, grace in ((signal.SIGTERM, 2), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def wait_or_cancel(proc, cancel, timeout):
    """等进程结束，或者取消、超时。返回 done / cancelled / timeout。
    只有进程确实是被我们杀掉的，才算取消或超时；它自己先结束了，就算完成。"""
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancel.is_set() or time.monotonic() > deadline:
            reason = "cancelled" if cancel.is_set() else "timeout"
            kill_group(proc)
            return reason if proc.returncode is not None and proc.returncode < 0 else "done"
        try:
            proc.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            pass
        except KeyboardInterrupt:        # 终端里按 Ctrl-C：转成取消信号
            cancel.set()
    return "done"


def confined(argv, workdir):
    """把模型能触发的命令放进 bubblewrap，.git 挂成只读：shell 里可以改文件，
    但写不进仓库，提交只能经过 commit 工具和它的闸门。没有 bwrap 就拒绝执行。"""
    if SANDBOX == "off":
        return argv
    if shutil.which("bwrap") is None:
        raise RuntimeError("沙箱不可用：找不到 bwrap。安装 bubblewrap，或设 RA_SANDBOX=off 明确关闭隔离")
    binds = ["--dev-bind", "/", "/"]
    if (workdir / ".git").exists():
        binds += ["--ro-bind", str(workdir / ".git"), str(workdir / ".git")]
    return ["bwrap", *binds, "--chdir", str(workdir), "--", *argv]


def run_command(command, ctx, timeout, confine=True):
    argv = ["bash", "-c", command]
    if confine:
        argv = confined(argv, ctx.workdir)
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(argv, cwd=ctx.workdir, stdout=out,
                                stderr=err, text=True, start_new_session=True)
        outcome = wait_or_cancel(proc, ctx.cancel, timeout)
        out.seek(0)
        err.seek(0)
        text = f"exit={proc.returncode}\n[stdout]\n{out.read()}\n[stderr]\n{err.read()}"
    if outcome == "cancelled":
        raise Cancelled(f"命令已被取消，进程组已终止。取消前的输出：\n{text}")
    if outcome == "timeout":
        return proc.returncode, f"超时（{timeout} 秒），进程组已终止。\n{text}"
    return proc.returncode, text


def run_bash(args, ctx):
    return run_command(args["command"], ctx, int(args.get("timeout", 120)))[1]


def tree_hash(workdir):
    """工作目录内容的哈希。跳过以点开头的文件和目录（.git、日志、缓存）以及 __pycache__。"""
    digest = hashlib.sha256()
    for path in sorted(workdir.rglob("*")):
        parts = path.relative_to(workdir).parts
        if any(p.startswith(".") or p == "__pycache__" for p in parts) or not path.is_file():
            continue
        digest.update("/".join(parts).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def run_tests(args, ctx):
    """跑验证命令。通过、并且验证前后内容没变，才记一条 verified，绑定到这份内容的哈希。"""
    before = tree_hash(ctx.workdir)
    code, text = run_command(VERIFY_CMD, ctx, 600)
    after = tree_hash(ctx.workdir)
    if code == 0 and before == after:
        ctx.journal.log("verified", hash=before, call_id=ctx.call_id)
        return f"验证通过（内容哈希 {before}）\n{text}"
    if code == 0:
        return f"验证命令通过，但运行期间文件被改动了，不算验证通过\n{text}"
    return f"验证失败\n{text}"


# 只暂存参与哈希的那些文件：与 tree_hash 跳过的范围一致
STAGE_CMD = ("git add -A -- . ':(exclude,glob)**/.*' ':(exclude,glob)**/.*/**' "
             "':(exclude,glob)**/__pycache__/**'")


def commit(args, ctx):
    """只有当前内容的哈希被验证过，才允许提交。"""
    current = tree_hash(ctx.workdir)
    if not ctx.journal.find("verified", hash=current):
        raise ValueError(f"当前内容（哈希 {current}）没有通过验证，拒绝提交。先运行 run_tests。")
    code, text = run_command(STAGE_CMD, ctx, 60, confine=False)
    if code != 0 or tree_hash(ctx.workdir) != current:
        raise ValueError(f"暂存失败或内容在暂存期间被改动，拒绝提交\n{text}")
    code, text = run_command(f"git commit -q -m {json.dumps(args['message'])}", ctx, 60,
                             confine=False)
    if code != 0:
        raise ValueError(f"git commit 失败\n{text}")
    ctx.journal.log("commit", hash=current, call_id=ctx.call_id)
    return f"已提交（内容哈希 {current}）"


class PaymentService:
    """演示用的外部服务：一个 JSONL 账本。支持幂等键时，同一个键只记一次账。"""

    def __init__(self, path, idempotent):
        self.path, self.idempotent = pathlib.Path(path), idempotent

    def entries(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def charge(self, to, amount, key):
        entries = self.entries()
        if self.idempotent:
            for entry in entries:
                if entry["key"] == key:
                    return entry["receipt"]       # 同一个键：返回上次的回执，不再扣款
        receipt = f"rcpt-{len(entries) + 1}"
        with self.path.open("a") as f:
            f.write(json.dumps({"to": to, "amount": amount, "receipt": receipt,
                                "key": key if self.idempotent else None}) + "\n")
        return receipt


def make_tools(payments):
    def pay(args, ctx):
        receipt = payments.charge(args["to"], args["amount"], ctx.key)
        return f"已向 {args['to']} 付款 {args['amount']}，回执 {receipt}"

    path = ("string", "相对工作目录的路径", False)
    tools = [
        Tool("read_file", "读取文本文件，带行号。",
             {"path": path, "offset": ("integer", "从第几行开始，默认 1", True),
              "limit": ("integer", "最多读多少行，默认 400", True)},
             read_file, sensitive=False, read_only=True),
        Tool("write_file", "创建或整个覆盖一个文件。",
             {"path": path, "content": ("string", "文件的完整新内容", False)},
             write_file, sensitive=True),
        Tool("edit_file", "把文件里恰好出现一次的 old 替换成 new。修改前先 read_file；"
                          "old 和 new 是文件原文，不要带 read_file 输出里的行号前缀。",
             {"path": path, "old": ("string", "要被替换的原文，必须在文件中唯一", False),
              "new": ("string", "替换后的文本", False)},
             edit_file, sensitive=True),
        Tool("run_bash", "在工作目录里执行一条 bash 命令，返回退出码和输出。",
             {"command": ("string", "要执行的命令", False),
              "timeout": ("integer", "超时秒数，默认 120", True)},
             run_bash, sensitive=True),
        Tool("run_tests", f"运行验证命令（{VERIFY_CMD}）。通过时记录当前内容的哈希。",
             {}, run_tests, sensitive=False),
        Tool("commit", "用 git 提交当前内容。当前内容必须已经通过 run_tests。",
             {"message": ("string", "提交说明", False)}, commit, sensitive=True),
        Tool("pay", "向收款方付款。",
             {"to": ("string", "收款方", False), "amount": ("number", "金额", False)},
             pay, sensitive=True, idempotent=payments.idempotent),
    ]
    return {tool.name: tool for tool in tools}


# ── 第 4 部分：审批与截断 ───────────────────────────────────────────

def terminal_approver(name, args):
    print(f"\n[需要确认] {name} {json.dumps(args, ensure_ascii=False)[:300]}")
    try:
        return input("允许吗？[y/N] ").strip().lower() == "y"
    except EOFError:                 # 没有人能回答，就当作拒绝
        return False


def clip(text):
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    head, tail = text[:MAX_TOOL_OUTPUT // 2], text[-MAX_TOOL_OUTPUT // 4:]
    return f"{head}\n…（中间省略 {len(text) - len(head) - len(tail)} 字符）…\n{tail}"


# ── 第 5 部分：循环 ────────────────────────────────────────────────

SYSTEM_PROMPT = """你是一个在终端里工作的编程助手，工作目录是 {workdir}。
你能通过工具读写这个目录里的文件、执行命令、运行验证、提交代码。
做事的规矩：
- 改文件之前先读它；只改完成任务所需的部分。
- 改完之后用 run_tests 验证；提交只能在验证通过之后，而且提交的必须是被验证过的那份内容。
- 工具报错时读懂错误再决定下一步，不要原样重试。
- 对话里的摘要只是提示，文件和测试的真实状态以工具结果为准。
- 任务完成后用一两句话说明你改了什么、怎样验证的。"""

NOT_STARTED = "这个工具调用在开始之前进程就中断了，它没有执行。如果仍然需要，可以重新调用。"
OUTCOME_UNKNOWN = ("这个工具调用已经开始，但结果没有记录下来就中断了，它可能已经生效。"
                   "不要直接重试：先确认外部状态，或者问用户。")
STOP_TEXT = {"denied": "未执行：用户拒绝了本轮更早的一次调用。",
             "cancelled": "未执行：本轮已被取消。"}


class Harness:
    def __init__(self, llm, journal, tools, approver, workdir, out=print,
                 retry_delays=RETRY_DELAYS):
        self.llm, self.journal, self.tools, self.approver = llm, journal, tools, approver
        self.workdir, self.out, self.retry_delays = pathlib.Path(workdir).resolve(), out, retry_delays
        self.cancel = threading.Event()
        if not journal.find("session"):
            journal.log("session", id=uuid.uuid4().hex[:12])
            journal.append({"role": "system",
                            "content": SYSTEM_PROMPT.format(workdir=self.workdir)})
        self.session_id = journal.find("session")[0]["id"]

    # 入口
    def run(self, user_text):
        self.cancel.clear()
        self.journal.log("turn_start")
        self.journal.append({"role": "user", "content": user_text})
        return self._loop()

    def resume(self):
        """上一轮没有结束（进程崩溃）：补齐缺失的工具结果，然后接着跑这一轮。"""
        if not self.journal.turn_open():
            return None
        last = self.journal.messages[-1]
        if last["role"] == "assistant" and not last.get("tool_calls"):
            return self._end("completed", last["content"])   # 只差一条 turn_end
        stop = self._repair()
        return self._end(stop) if stop else self._loop()

    # 循环
    def _loop(self):
        while self.journal.steps_in_turn() < MAX_STEPS:
            if self.cancel.is_set():
                return self._end("cancelled")
            if estimate_tokens(self.journal.messages) > CONTEXT_BUDGET and self._compact():
                self.out("（上下文已压缩）")
            try:
                reply = self._chat()
            except LLMError as error:
                return self._end("error", f"模型调用失败：{error}")
            except KeyboardInterrupt:
                return self._end("cancelled")
            message = {"role": "assistant", "content": reply.get("content") or ""}
            if reply.get("tool_calls"):
                message["tool_calls"] = reply["tool_calls"]
            self.journal.append(message)
            calls = reply.get("tool_calls") or []
            if not calls:
                return self._end("completed", message["content"])
            stop = None
            for call in calls:
                if stop:                         # 被拒或被取消之后，同一批里剩下的都不执行
                    self._result(call, STOP_TEXT[stop])
                else:
                    stop = self._execute(call)
            if stop:
                return self._end(stop)
        return self._end("max_steps")

    def _chat(self):
        for attempt in range(len(self.retry_delays) + 1):
            try:
                return self.llm.chat(self.journal.messages,
                                     [tool.schema for tool in self.tools.values()])
            except LLMError as error:
                if not error.transient or attempt == len(self.retry_delays):
                    raise
                self.journal.log("llm_retry", attempt=attempt + 1, error=str(error))
                time.sleep(self.retry_delays[attempt])

    def _end(self, reason, text=None):
        self.journal.log("turn_end", reason=reason)
        notes = {"denied": "（已拒绝，本轮停止。告诉我接下来该怎么做。）",
                 "cancelled": "（本轮已取消。）",
                 "max_steps": f"（达到 {MAX_STEPS} 步上限，本轮停止）"}
        self.out(text if text is not None else notes.get(reason, f"（本轮结束：{reason}）"))
        return reason

    # 工具调用
    def _execute(self, call):
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("参数必须是一个 JSON 对象")
        except ValueError as error:
            return self._result(call, f"错误：参数不是合法的 JSON 对象（{error}）")
        tool = self.tools.get(name)
        if tool is None:
            return self._result(call, f"错误：没有叫 {name} 的工具")
        if self.cancel.is_set():
            self._result(call, STOP_TEXT["cancelled"])
            return "cancelled"
        if tool.sensitive:
            granted = bool(self.approver(name, args))
            self.journal.log("approval", call_id=call["id"], granted=granted)
            if not granted:
                self._result(call, "用户拒绝了这次调用。")
                return "denied"
            if self.cancel.is_set():             # 等审批的时候用户取消了
                self._result(call, STOP_TEXT["cancelled"])
                return "cancelled"
        return self._dispatch(call, tool, args, retry=False)

    def _dispatch(self, call, tool, args, retry):
        self.out(f"[{self.journal.steps_in_turn()}] {tool.name} "
                 f"{json.dumps(args, ensure_ascii=False)[:120]}")
        key = f"{self.session_id}:{call['id']}"
        crash_point(f"before_start:{tool.name}")
        self.journal.log("tool_start", call_id=call["id"], name=tool.name, key=key, retry=retry)
        crash_point(f"after_start:{tool.name}")
        ctx = ExecCtx(self.workdir, key, self.cancel, self.journal, call["id"])
        try:
            result = tool.impl(args, ctx)
        except Cancelled as cancelled:
            self._result(call, str(cancelled))
            return "cancelled"
        except Exception as error:              # 工具出错也要回给模型
            result = f"错误：{type(error).__name__}: {error}"
        crash_point(f"after_execute:{tool.name}")
        return self._result(call, result)

    def _result(self, call, text):
        self.journal.append({"role": "tool", "tool_call_id": call["id"], "content": clip(text)})
        return None

    def _repair(self):
        """给最后一条 assistant 消息里没有结果的调用补上结果。"""
        positions = [i for i, m in enumerate(self.journal.messages) if m["role"] == "assistant"]
        if not positions:
            return None
        index = positions[-1]
        answered = {m.get("tool_call_id") for m in self.journal.messages[index + 1:]}
        for call in self.journal.messages[index].get("tool_calls") or []:
            if call["id"] in answered:
                continue
            tool = self.tools.get(call["function"]["name"])
            approved = not (tool and tool.sensitive) or \
                self.journal.find("approval", call_id=call["id"], granted=True)
            if not self.journal.find("tool_start", call_id=call["id"]):
                self.journal.log("repair", call_id=call["id"], outcome="not_started")
                self._result(call, NOT_STARTED)
            elif tool and (tool.read_only or tool.idempotent) and approved:
                # 只读，或者带同一个幂等键重做也只会生效一次：直接重做，拿到真实结果
                self.journal.log("repair", call_id=call["id"], outcome="redo")
                stop = self._dispatch(call, tool, json.loads(call["function"]["arguments"]), True)
                if stop:
                    return stop
            else:
                self.journal.log("repair", call_id=call["id"], outcome="unknown")
                self._result(call, OUTCOME_UNKNOWN)
        return None

    # 压缩
    def _compact(self):
        msgs = self.journal.messages
        user_positions = [i for i, m in enumerate(msgs) if m["role"] == "user" and i > 0]
        if len(user_positions) <= KEEP_RECENT_TURNS:
            return False
        cut = user_positions[-KEEP_RECENT_TURNS]
        transcript = json.dumps(msgs[1:cut], ensure_ascii=False)[-60000:]
        summary = self.llm.chat([
            {"role": "system", "content": "你负责给编程助手的工作记录写交接摘要。"},
            {"role": "user", "content": "下面是一段工作记录（JSON）。用不超过 300 字总结："
             "用户要什么、改了哪些文件、哪些已验证、还剩什么没做。\n\n" + transcript},
        ])["content"]
        note = {"role": "user", "content": "[以下是更早对话的摘要，只作提示，"
                f"文件与测试的真实状态以工具结果为准]\n{summary}"}
        ack = {"role": "assistant", "content": "好的，我已了解之前的进展。"}
        self.journal.log("compaction", messages=[msgs[0], note, ack] + msgs[cut:])
        return True


def estimate_tokens(messages):
    return len(json.dumps(messages, ensure_ascii=False)) // 3


def main():
    parser = argparse.ArgumentParser(description="带崩溃恢复和提交闸门的 coding agent")
    parser.add_argument("prompt", nargs="?", help="单次任务；省略则进入交互模式")
    parser.add_argument("--yolo", action="store_true", help="不确认，直接执行")
    parser.add_argument("--session", default=".reliable_agent_session.jsonl")
    args = parser.parse_args()

    workdir = pathlib.Path.cwd().resolve()
    llm = OpenAIChat(os.environ.get("MINI_AGENT_BASE_URL", "http://127.0.0.1:11434/v1"),
                     os.environ.get("MINI_AGENT_MODEL", "local-model"),
                     os.environ.get("MINI_AGENT_API_KEY", "none"))
    payments = PaymentService(os.environ.get("RA_PAY_LEDGER", workdir / ".payments.jsonl"),
                              idempotent=os.environ.get("RA_PAY_IDEMPOTENT", "1") == "1")
    approver = (lambda name, a: True) if args.yolo else terminal_approver
    harness = Harness(llm, Journal(args.session), make_tools(payments), approver, workdir)
    if harness.resume():
        print("（已接着上次中断的那一轮跑完）")
    if args.prompt:
        harness.run(args.prompt)
        return
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if text:
            harness.run(text)


if __name__ == "__main__":
    sys.exit(main())
