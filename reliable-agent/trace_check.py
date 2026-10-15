"""对一份真实的会话日志检查几条性质。它们和 AgentLoop.tla 里检查的不变式一一对应。"""
import json
import pathlib


def load(path):
    return [json.loads(line) for line in pathlib.Path(path).read_text().splitlines()]


def check(records, sensitive, finished=True):
    """返回违反的性质列表；空列表表示全部满足。

    sensitive: 需要审批的工具名集合。
    finished:  日志是否应该已经停在一轮结束的地方。
    """
    problems = []
    approvals, verified = {}, set()
    requested, answered = [], {}
    open_turn = False
    last_batch = []          # 最近一条带工具调用的 assistant 消息里的调用
    for index, record in enumerate(records):
        kind = record["kind"]
        if kind == "approval":
            approvals[record["call_id"]] = record["granted"]
        elif kind == "verified":
            verified.add(record["hash"])
        elif kind == "tool_start":
            # AuthOK：敏感工具开始执行之前，必须有这次调用的批准记录
            if record["name"] in sensitive and approvals.get(record["call_id"]) is not True:
                problems.append(f"#{index} AuthOK：{record['name']} 未经批准就开始执行")
        elif kind == "commit":
            # CommitGate：提交的内容哈希必须在此之前被验证过
            if record["hash"] not in verified:
                problems.append(f"#{index} CommitGate：提交了未验证的内容 {record['hash']}")
        elif kind == "turn_start":
            if open_turn:
                problems.append(f"#{index} 上一轮没有 turn_end 就开始了新一轮")
            open_turn = True
        elif kind == "turn_end":
            open_turn = False
        elif kind == "message":
            message = record["message"]
            if message.get("tool_calls"):
                last_batch = [call["id"] for call in message["tool_calls"]]
            for call in message.get("tool_calls") or []:
                requested.append(call["id"])
            if message["role"] == "tool":
                answered[message["tool_call_id"]] = answered.get(message["tool_call_id"], 0) + 1
    # Paired：每个工具调用恰好有一条结果。日志停在一轮中间（进程崩溃）时，
    # 最后那一批调用可以还没有结果，等恢复时补上
    pending_ok = set(last_batch) if (open_turn and not finished) else set()
    for call_id in requested:
        count = answered.get(call_id, 0)
        if count == 0 and call_id in pending_ok:
            continue
        if count != 1:
            problems.append(f"Paired：调用 {call_id} 有 {answered.get(call_id, 0)} 条结果")
    for call_id in answered:
        if call_id not in requested:
            problems.append(f"Paired：结果 {call_id} 没有对应的调用")
    # Terminates（有限轨迹上的版本）：日志停在一轮结束的地方
    if finished and open_turn:
        problems.append("Terminates：最后一轮没有 turn_end")
    return problems
