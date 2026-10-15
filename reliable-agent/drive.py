"""在子进程里跑 harness，供崩溃测试使用：python3 drive.py <场景> <工作目录> <会话文件> <账本> <幂等 0/1>

场景决定假模型的脚本。进程可能在 RA_CRASH_AT 指定的位置被杀掉；
用同样的参数再启动一次，harness 会先修补上次中断的那一轮再接着跑。
"""
import pathlib
import sys

import fake_llm as f
import reliable_agent as ra

SCENARIOS = {
    # 付一次款，然后报告
    "pay": [f.calls(f.call("p1", "pay", {"to": "alice", "amount": 30})), f.text("已付款")],
    # 写一个文件，然后报告
    "write": [f.calls(f.call("w1", "write_file", {"path": "out.txt", "content": "hello\n"})),
              f.text("已写入")],
}


def main():
    scenario, workdir, session, ledger, idempotent = sys.argv[1:6]
    payments = ra.PaymentService(ledger, idempotent=idempotent == "1")
    harness = ra.Harness(f.ScriptedLLM(SCENARIOS[scenario]), ra.Journal(session),
                         ra.make_tools(payments), lambda name, args: True,
                         pathlib.Path(workdir), retry_delays=[0, 0, 0])
    if harness.resume() is None:
        harness.run(f"执行场景 {scenario}")


if __name__ == "__main__":
    main()
