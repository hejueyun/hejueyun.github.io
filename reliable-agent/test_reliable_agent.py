"""用假模型和故障注入测试 harness。每个测试结束时都用 trace_check 检查真实日志。"""
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

import fake_llm as f
import reliable_agent as ra
import trace_check

HERE = pathlib.Path(__file__).resolve().parent


class Env:
    def __init__(self, tmp_path, script, approver=None, idempotent=True, by_count=True):
        self.workdir = tmp_path / "work"
        self.workdir.mkdir()
        self.session = tmp_path / "session.jsonl"
        self.ledger = tmp_path / "ledger.jsonl"
        self.payments = ra.PaymentService(self.ledger, idempotent)
        self.tools = ra.make_tools(self.payments)
        self.llm = f.ScriptedLLM(script, by_count=by_count)
        self.output = []
        self.harness = ra.Harness(self.llm, ra.Journal(self.session), self.tools,
                                  approver or (lambda name, args: True), self.workdir,
                                  out=self.output.append, retry_delays=[0, 0, 0])

    def records(self):
        return trace_check.load(self.session)

    def kinds(self, kind):
        return [r for r in self.records() if r["kind"] == kind]

    def tool_results(self):
        return [r["message"]["content"] for r in self.records()
                if r["kind"] == "message" and r["message"]["role"] == "tool"]

    def sensitive(self):
        return {name for name, tool in self.tools.items() if tool.sensitive}

    def assert_trace_ok(self, finished=True):
        assert trace_check.check(self.records(), self.sensitive(), finished) == []


# ── 循环本身 ────────────────────────────────────────────────────────

def test_text_reply_ends_turn(tmp_path):
    env = Env(tmp_path, [f.text("你好")])
    assert env.harness.run("hi") == "completed"
    assert env.output == ["你好"]
    env.assert_trace_ok()


def test_multiple_calls_are_answered_in_order(tmp_path):
    env = Env(tmp_path, [f.calls(f.call("a", "read_file", {"path": "x.txt"}),
                                 f.call("b", "read_file", {"path": "y.txt"})),
                         f.text("读完了")])
    (env.workdir / "x.txt").write_text("X\n")
    (env.workdir / "y.txt").write_text("Y\n")
    env.harness.run("读两个文件")
    tools = [r["message"] for r in env.records()
             if r["kind"] == "message" and r["message"]["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["a", "b"]
    assert "X" in tools[0]["content"] and "Y" in tools[1]["content"]
    env.assert_trace_ok()


def test_bad_arguments_and_unknown_tools_become_results(tmp_path):
    env = Env(tmp_path, [f.calls(f.call("a", "read_file", "{oops"),
                                 f.call("b", "read_file", "[1, 2]"),
                                 f.call("c", "rm_rf", {})),
                         f.text("我换个做法")])
    assert env.harness.run("试试") == "completed"
    a, b, c = env.tool_results()
    assert "不是合法的 JSON 对象" in a and "不是合法的 JSON 对象" in b
    assert "没有叫 rm_rf 的工具" in c
    env.assert_trace_ok()


def test_tool_exception_is_returned_to_model(tmp_path):
    env = Env(tmp_path, [f.calls(f.call("a", "edit_file",
                                        {"path": "x.txt", "old": "nope", "new": "yes"})),
                         f.text("没找到")])
    (env.workdir / "x.txt").write_text("hello\n")
    env.harness.run("改一下")
    assert "出现了 0 次" in env.tool_results()[0]
    env.assert_trace_ok()


def test_step_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "MAX_STEPS", 3)
    script = [f.calls(f.call(f"c{i}", "read_file", {"path": "x.txt"})) for i in range(10)]
    env = Env(tmp_path, script)
    (env.workdir / "x.txt").write_text("x\n")
    assert env.harness.run("一直读") == "max_steps"
    assert len(env.tool_results()) == 3
    env.assert_trace_ok()


# ── 模型调用失败 ────────────────────────────────────────────────────

def test_transient_errors_are_retried(tmp_path):
    env = Env(tmp_path, [[f.transient(), f.transient(), f.text("终于好了")]])
    assert env.harness.run("hi") == "completed"
    assert len(env.kinds("llm_retry")) == 2
    env.assert_trace_ok()


def test_permanent_error_ends_turn_without_retry(tmp_path):
    env = Env(tmp_path, [[f.permanent()]])
    assert env.harness.run("hi") == "error"
    assert env.kinds("llm_retry") == []
    env.assert_trace_ok()


def test_retries_are_bounded(tmp_path):
    env = Env(tmp_path, [[f.transient()]])
    assert env.harness.run("hi") == "error"
    assert len(env.kinds("llm_retry")) == 3
    env.assert_trace_ok()


# ── 审批与取消 ──────────────────────────────────────────────────────

def test_denial_stops_the_rest_of_the_batch(tmp_path):
    env = Env(tmp_path, [f.calls(f.call("a", "write_file", {"path": "a.txt", "content": "1"}),
                                 f.call("b", "write_file", {"path": "b.txt", "content": "2"}))],
              approver=lambda name, args: False)
    assert env.harness.run("写两个文件") == "denied"
    assert not (env.workdir / "a.txt").exists() and not (env.workdir / "b.txt").exists()
    assert env.tool_results() == ["用户拒绝了这次调用。", ra.STOP_TEXT["denied"]]
    assert env.kinds("tool_start") == []
    env.assert_trace_ok()


def test_cancel_during_approval_prevents_execution(tmp_path):
    holder = {}

    def approver(name, args):
        holder["harness"].cancel.set()          # 用户在审批提示出现时按了 Ctrl-C
        return True

    env = Env(tmp_path, [f.calls(f.call("a", "write_file", {"path": "a.txt", "content": "1"}))],
              approver=approver)
    holder["harness"] = env.harness
    assert env.harness.run("写") == "cancelled"
    assert not (env.workdir / "a.txt").exists()
    env.assert_trace_ok()


def test_cancel_kills_the_whole_process_group(tmp_path):
    command = "sleep 30 & echo $! > child.pid; wait"
    env = Env(tmp_path, [f.calls(f.call("a", "run_bash", {"command": command}))])
    threading.Timer(1.0, env.harness.cancel.set).start()
    started = time.monotonic()
    assert env.harness.run("跑个长命令") == "cancelled"
    assert time.monotonic() - started < 10
    child = int((env.workdir / "child.pid").read_text())
    time.sleep(0.2)
    assert not pid_alive(child), "后台子进程还活着"
    assert "命令已被取消" in env.tool_results()[0]
    env.assert_trace_ok()


def test_completion_wins_over_late_cancel(tmp_path):
    proc = subprocess.Popen(["bash", "-c", "exit 3"], start_new_session=True)
    time.sleep(0.3)                             # 进程已经结束，但还没有被回收
    cancel = threading.Event()
    cancel.set()
    assert ra.wait_or_cancel(proc, cancel, timeout=5) == "done"
    assert proc.returncode == 3


def test_timeout_kills_process_group(tmp_path):
    env = Env(tmp_path, [f.calls(f.call("a", "run_bash", {"command": "sleep 30", "timeout": 1})),
                         f.text("超时了")])
    started = time.monotonic()
    env.harness.run("跑")
    assert time.monotonic() - started < 10
    assert env.tool_results()[0].startswith("超时（1 秒）")
    env.assert_trace_ok()


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = pathlib.Path(f"/proc/{pid}/status")
    return not (status.exists() and "State:\tZ" in status.read_text())


# ── 提交闸门 ────────────────────────────────────────────────────────

VERIFY = "python3 -c \"import sys; sys.exit(0 if open('ok.txt').read().strip() == 'ok' else 1)\""


def git_repo(workdir):
    for command in ("git init -q", "git config user.email t@t", "git config user.name t"):
        subprocess.run(command, shell=True, cwd=workdir, check=True)


def commit_count(workdir):
    done = subprocess.run("git rev-list --count HEAD", shell=True, cwd=workdir,
                          capture_output=True, text=True)
    return int(done.stdout) if done.returncode == 0 else 0


def test_commit_requires_the_verified_content(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "VERIFY_CMD", VERIFY)
    write = lambda cid, text: f.calls(f.call(cid, "write_file", {"path": "ok.txt", "content": text}))
    commit = lambda cid: f.calls(f.call(cid, "commit", {"message": cid}))
    env = Env(tmp_path, [
        write("w1", "ok\n"), commit("c1"),                       # 没验证就提交：拒绝
        f.calls(f.call("t1", "run_tests", {})), commit("c2"),    # 验证后提交：通过
        write("w2", "ok\n# 改动\n"), commit("c3"),               # 验证后又改了：拒绝
        f.text("完成"),
    ])
    git_repo(env.workdir)
    env.harness.run("改、测、提交")
    results = env.tool_results()
    assert "没有通过验证，拒绝提交" in results[1]
    assert results[2].startswith("验证通过") and results[3].startswith("已提交")
    assert "没有通过验证，拒绝提交" in results[5]
    assert len(env.kinds("commit")) == 1 and commit_count(env.workdir) == 1
    env.assert_trace_ok()


def test_shell_cannot_commit_around_the_gate(tmp_path):
    """模型想用 run_bash 直接 git commit，绕过提交闸门：.git 是只读的，写不进去。"""
    env = Env(tmp_path, [f.calls(f.call("s1", "run_bash",
                                        {"command": "echo x > a.txt && git add -A && git commit -qm sneaky"})),
                         f.text("提交不了")])
    git_repo(env.workdir)
    env.harness.run("提交")
    assert "Read-only file system" in env.tool_results()[0]
    assert (env.workdir / "a.txt").exists()                # 普通文件照常可写
    assert commit_count(env.workdir) == 0
    env.assert_trace_ok()


def test_missing_sandbox_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(ra.shutil, "which", lambda name: None)
    env = Env(tmp_path, [f.calls(f.call("s1", "run_bash", {"command": "touch a.txt"})), f.text("嗯")])
    env.harness.run("跑")
    assert "沙箱不可用" in env.tool_results()[0]
    assert not (env.workdir / "a.txt").exists()
    env.assert_trace_ok()


def test_verifier_that_changes_files_does_not_count(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "VERIFY_CMD", "echo extra > generated.txt")
    env = Env(tmp_path, [f.calls(f.call("t1", "run_tests", {})), f.text("嗯")])
    env.harness.run("测")
    assert "运行期间文件被改动了" in env.tool_results()[0]
    assert env.kinds("verified") == []
    env.assert_trace_ok()


def test_summary_claim_does_not_unlock_commit(tmp_path, monkeypatch):
    """压缩之后，摘要里写着测试通过了；但提交闸门看的是日志里的 verified 记录。"""
    monkeypatch.setattr(ra, "VERIFY_CMD", VERIFY)
    monkeypatch.setattr(ra, "KEEP_RECENT_TURNS", 1)
    env = Env(tmp_path, [
        f.calls(f.call("w1", "write_file", {"path": "ok.txt", "content": "ok\n"})),
        f.calls(f.call("t1", "run_tests", {})),
        f.calls(f.call("w2", "write_file", {"path": "ok.txt", "content": "broken\n"})),
        f.text("第一轮结束"),
        f.calls(f.call("c1", "commit", {"message": "x"})),
        f.text("提交被拒绝了"),
    ], by_count=False)
    env.llm.summary = "用户要求修复 ok.txt。已修改 ok.txt，测试已全部通过，可以直接提交。"
    git_repo(env.workdir)
    env.harness.run("第一轮")
    monkeypatch.setattr(ra, "CONTEXT_BUDGET", 10)          # 第二轮一开始就压缩
    env.harness.run("提交吧")
    assert len(env.kinds("compaction")) >= 1
    assert "测试已全部通过" in json.dumps(env.harness.journal.messages, ensure_ascii=False)
    assert "没有通过验证，拒绝提交" in env.tool_results()[-1]
    assert commit_count(env.workdir) == 0
    env.assert_trace_ok()


# ── 进程崩溃 ────────────────────────────────────────────────────────

def drive(tmp_path, scenario, idempotent, crash_at=None):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    env = dict(os.environ, RA_CRASH_AT=crash_at or "")
    args = [sys.executable, str(HERE / "drive.py"), scenario, str(workdir),
            str(tmp_path / "session.jsonl"), str(tmp_path / "ledger.jsonl"),
            "1" if idempotent else "0"]
    return subprocess.run(args, env=env, cwd=HERE, capture_output=True, text=True).returncode


def crash_then_resume(tmp_path, scenario, idempotent, crash_at):
    assert drive(tmp_path, scenario, idempotent, crash_at) == 137
    assert trace_check.check(trace_check.load(tmp_path / "session.jsonl"),
                             ra_sensitive(), finished=False) == []
    assert drive(tmp_path, scenario, idempotent) == 0
    records = trace_check.load(tmp_path / "session.jsonl")
    assert trace_check.check(records, ra_sensitive()) == []
    ledger = ra.PaymentService(tmp_path / "ledger.jsonl", idempotent).entries()
    repairs = [r["outcome"] for r in records if r["kind"] == "repair"]
    results = [r["message"]["content"] for r in records
               if r["kind"] == "message" and r["message"]["role"] == "tool"]
    return ledger, repairs, results


def ra_sensitive():
    return {n for n, t in ra.make_tools(ra.PaymentService("/dev/null", True)).items() if t.sensitive}


def test_crash_after_payment_idempotent_service(tmp_path):
    ledger, repairs, results = crash_then_resume(tmp_path, "pay", True, "after_execute:pay")
    assert len(ledger) == 1                      # 重做了一次，但只扣了一次款
    assert repairs == ["redo"]
    assert "rcpt-1" in results[0]


def test_crash_after_payment_non_idempotent_service(tmp_path):
    ledger, repairs, results = crash_then_resume(tmp_path, "pay", False, "after_execute:pay")
    assert len(ledger) == 1                      # 不重做：结果未知，交给模型和人
    assert repairs == ["unknown"]
    assert results == [ra.OUTCOME_UNKNOWN]


def test_crash_before_side_effect_is_still_unknown(tmp_path):
    """tool_start 已经落盘、付款还没发出时崩溃：harness 分不清这两种情况，只能当作结果未知。"""
    ledger, repairs, results = crash_then_resume(tmp_path, "pay", False, "after_start:pay")
    assert ledger == [] and repairs == ["unknown"]


def test_crash_before_start_is_not_started(tmp_path):
    ledger, repairs, results = crash_then_resume(tmp_path, "write", False, "before_start:write_file")
    assert repairs == ["not_started"] and results == [ra.NOT_STARTED]
    assert not (tmp_path / "work" / "out.txt").exists()


def test_naive_redo_would_double_charge(tmp_path):
    """对照组：如果恢复时不管三七二十一重做一遍，不支持幂等键的服务会被扣两次款。"""
    class NaiveHarness(ra.Harness):
        def _repair(self):
            last = [m for m in self.journal.messages if m["role"] == "assistant"][-1]
            for call in last["tool_calls"]:
                tool = self.tools[call["function"]["name"]]
                self._dispatch(call, tool, json.loads(call["function"]["arguments"]), True)

    assert drive(tmp_path, "pay", False, "after_execute:pay") == 137
    payments = ra.PaymentService(tmp_path / "ledger.jsonl", idempotent=False)
    naive = NaiveHarness(f.ScriptedLLM([None, f.text("已付款")]),
                         ra.Journal(tmp_path / "session.jsonl"), ra.make_tools(payments),
                         lambda name, args: True, tmp_path / "work", out=lambda s: None)
    naive.resume()
    assert len(payments.entries()) == 2


def test_half_written_last_line_is_dropped(tmp_path):
    session = tmp_path / "s.jsonl"
    journal = ra.Journal(session)
    journal.log("turn_start")
    with session.open("a") as fp:
        fp.write('{"kind": "message", "mess')      # 崩溃时只写了半行
    journal = ra.Journal(session)
    assert [r["kind"] for r in journal.records] == ["turn_start"]
    journal.log("turn_end", reason="completed")
    assert [r["kind"] for r in trace_check.load(session)] == ["turn_start", "turn_end"]


def test_corruption_in_the_middle_is_an_error(tmp_path):
    session = tmp_path / "s.jsonl"
    session.write_text('{"kind": "turn_start"}\n{broken\n{"kind": "turn_end"}\n')
    with pytest.raises(ValueError, match="损坏"):
        ra.Journal(session)


# ── 检查器自己必须能发现违反 ────────────────────────────────────────

def msg(message):
    return {"kind": "message", "message": message}


def test_checker_detects_each_violation():
    unapproved = [{"kind": "turn_start"},
                  {"kind": "tool_start", "call_id": "a", "name": "pay"}]
    assert any("AuthOK" in p for p in trace_check.check(unapproved, {"pay"}, False))
    denied = [{"kind": "approval", "call_id": "a", "granted": False},
              {"kind": "tool_start", "call_id": "a", "name": "pay"}]
    assert any("AuthOK" in p for p in trace_check.check(denied, {"pay"}, False))
    unverified = [{"kind": "verified", "hash": "h1"}, {"kind": "commit", "hash": "h2"}]
    assert any("CommitGate" in p for p in trace_check.check(unverified, set(), False))
    unanswered = [msg({"role": "assistant", "content": "",
                       "tool_calls": [f.call("a", "read_file", {})]})]
    assert any("Paired" in p for p in trace_check.check(unanswered, set(), False))
    twice = unanswered + [msg({"role": "tool", "tool_call_id": "a", "content": "1"})] * 2
    assert any("Paired" in p for p in trace_check.check(twice, set(), False))
    closed_unanswered = [{"kind": "turn_start"}] + unanswered + [{"kind": "turn_end"}]
    assert any("Paired" in p for p in trace_check.check(closed_unanswered, set(), False))
    hanging = [{"kind": "turn_start"}]
    assert any("Terminates" in p for p in trace_check.check(hanging, set(), True))
