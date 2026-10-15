"""变异测试：每次往 reliable_agent.py 里故意放进一个缺陷，看测试套件能不能发现。

用法：python3 mutate.py   （在本目录下运行；变异体放在临时目录里，不改动本目录的文件）
"""
import pathlib
import shutil
import subprocess
import tempfile

MUTANTS = {
    "提交闸门不查哈希": ('    if not ctx.journal.find("verified", hash=current):', '    if False:'),
    "恢复时一律重做": ('            elif tool and (tool.read_only or tool.idempotent) and approved:',
                '            elif True:'),
    "只杀主进程不杀进程组": ('            os.killpg(proc.pid, sig)', '            os.kill(proc.pid, sig)'),
    "审批后不再检查取消": ('            if self.cancel.is_set():             # 等审批的时候用户取消了',
                  '            if False:'),
    "shell 里 .git 可写": ('    if (workdir / ".git").exists():', '    if False:'),
    "拒绝后继续执行同批调用": ('                if stop:                         # 被拒或被取消之后，同一批里剩下的都不执行',
                    '                if stop == "never":'),
}

here = pathlib.Path(__file__).resolve().parent
source = (here / "reliable_agent.py").read_text()
with tempfile.TemporaryDirectory() as root:
    for name, (old, new) in MUTANTS.items():
        assert source.count(old) == 1, f"{name}：要替换的代码不唯一或已不存在"
        workdir = pathlib.Path(root) / name
        workdir.mkdir()
        for helper in ("fake_llm.py", "trace_check.py", "drive.py", "test_reliable_agent.py"):
            shutil.copy(here / helper, workdir / helper)
        (workdir / "reliable_agent.py").write_text(source.replace(old, new))
        done = subprocess.run(["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf"],
                              cwd=workdir, capture_output=True, text=True, timeout=300)
        failed = [line.split("::")[1].split(" ")[0]
                  for line in done.stdout.splitlines() if line.startswith("FAILED")]
        print(f"{name}：{done.stdout.strip().splitlines()[-1]}")
        print(f"    失败的测试：{', '.join(failed) or '（没有，这个缺陷没被发现）'}")
