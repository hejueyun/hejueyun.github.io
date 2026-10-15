"""确定性的假模型：按脚本返回回复、工具调用或错误，用来测试 harness 而不是测试模型。"""
import json

from reliable_agent import LLMError


def text(content):
    return {"role": "assistant", "content": content}


def call(call_id, name, arguments):
    """arguments 是 dict 时转成 JSON；是 str 时原样使用，用来构造非法参数。"""
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def calls(*items, content=""):
    return {"role": "assistant", "content": content, "tool_calls": list(items)}


def transient(message="timeout"):
    return LLMError(message, transient=True)


def permanent(message="HTTP 400"):
    return LLMError(message, transient=False)


class ScriptedLLM:
    """第 n 次被要求回复时，用脚本里的第 n 项回答。

    默认按「对话里已有几条 assistant 消息」决定 n。这样进程崩溃、重启之后，
    同一个脚本会从断开的那一步接着回答。一项可以是一个列表，表示同一步的
    多次尝试，例如 [transient(), transient(), text("好")]：前两次报错，第三次成功。
    """

    def __init__(self, script, summary="（摘要）", by_count=True):
        self.script, self.summary, self.by_count = script, summary, by_count
        self.position, self.attempts, self.requests = 0, {}, []

    def chat(self, messages, tools=None):
        if tools is None:                       # 压缩时的总结请求
            return text(self.summary)
        self.requests.append(messages)
        if self.by_count:
            n = sum(1 for m in messages if m["role"] == "assistant")
        else:
            n = self.position
        item = self.script[n]
        if isinstance(item, list):
            attempt = self.attempts.get(n, 0)
            self.attempts[n] = attempt + 1
            item = item[min(attempt, len(item) - 1)]
        if isinstance(item, Exception):
            raise item
        if not self.by_count:
            self.position += 1
        return item
