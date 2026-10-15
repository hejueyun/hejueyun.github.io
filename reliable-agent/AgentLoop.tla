----------------------------- MODULE AgentLoop -----------------------------
(* reliable_agent.py 里一轮对话的抽象模型。

   模型（LLM）的选择、工具的结果、进程崩溃、人的审批都是非确定的；
   harness 的反应是确定的。TLC 穷举所有交错，检查四条不变式和一条活性。

   几个开关用来构造「有毛病的设计」，看 TLC 能不能找出反例：
     StepBound          一轮的步数有没有上限
     CrashBound         崩溃次数有没有上限（这是对环境的假设，不是设计）
     IdempotentPay      付款服务支不支持幂等键
     NaiveRedo          恢复时是否不管三七二十一重做已开始的调用
     ApprovalInJournal  审批结果是否落盘；否则崩溃后丢失，重做时也不再检查
     GateBindsHash      提交闸门是否检查「当前内容」被验证过，否则只检查「验证过某个版本」
     ShellConfined      shell 里 .git 是否只读；否则 shell 命令可以绕过闸门直接提交 *)
EXTENDS Naturals

CONSTANTS StepBound, CrashBound, IdempotentPay, NaiveRedo, ApprovalInJournal,
          GateBindsHash, ShellConfined, MaxSteps, MaxCrashes, MaxRetries

Hashes    == {"h1", "h2"}                         \* 工作目录内容的两种可能
Kinds     == {"read", "edit", "shell", "test", "commit", "pay"}
Sensitive == {"edit", "shell", "commit", "pay"}    \* 执行前要审批
Phases    == {"model", "approval", "start", "exec", "result", "crashed", "done", "failed"}

\* 已经开始但结果未知时，可以安全重做的调用
Repeatable(k) == k = "read" \/ (k = "pay" /\ IdempotentPay)

VARIABLES
    phase,      \* 这一轮走到哪儿了
    kind,       \* 当前工具调用的种类
    approved,   \* 当前调用是否获批（harness 记得的）
    started,    \* 当前调用的 tool_start 是否已落盘
    recorded,   \* 当前调用的结果是否已落盘
    steps, crashes, retries,
    pays,       \* 外部世界：当前这次付款调用实际扣了几次款
    keyUsed,    \* 外部世界：付款服务是否见过当前调用的幂等键
    work,       \* 工作目录当前内容的哈希
    verified,   \* 验证通过过的内容哈希
    committed   \* 提交过的内容哈希

vars == <<phase, kind, approved, started, recorded, steps, crashes, retries,
          pays, keyUsed, work, verified, committed>>

TypeOK ==
    /\ phase \in Phases
    /\ kind \in Kinds \cup {"none"}
    /\ approved \in BOOLEAN /\ started \in BOOLEAN /\ recorded \in BOOLEAN
    /\ steps \in 0..MaxSteps /\ crashes \in 0..MaxCrashes /\ retries \in 0..MaxRetries
    /\ pays \in 0..3 /\ keyUsed \in BOOLEAN
    /\ work \in Hashes /\ verified \subseteq Hashes /\ committed \subseteq Hashes

Init ==
    /\ phase = "model" /\ kind = "none"
    /\ approved = FALSE /\ started = FALSE /\ recorded = FALSE
    /\ steps = 0 /\ crashes = 0 /\ retries = 0
    /\ pays = 0 /\ keyUsed = FALSE
    /\ work = "h1" /\ verified = {} /\ committed = {}

world == <<pays, keyUsed, work, verified, committed>>

(* ── 模型的回复（非确定） ── *)
ModelText ==                                      \* 回一段文字：本轮完成
    /\ phase = "model" /\ phase' = "done"
    /\ UNCHANGED <<kind, approved, started, recorded, steps, crashes, retries>>
    /\ UNCHANGED world

ModelCall ==                                      \* 要求调用一个工具
    /\ phase = "model"
    /\ StepBound => steps < MaxSteps
    /\ \E k \in Kinds:
        /\ kind' = k
        /\ phase' = IF k \in Sensitive THEN "approval" ELSE "start"
    /\ steps' = IF StepBound THEN steps + 1 ELSE steps
    /\ approved' = FALSE /\ started' = FALSE /\ recorded' = FALSE
    /\ retries' = 0 /\ pays' = 0 /\ keyUsed' = FALSE
    /\ UNCHANGED <<crashes, work, verified, committed>>

StepLimit ==                                      \* 步数用完：本轮结束
    /\ phase = "model" /\ StepBound /\ steps = MaxSteps
    /\ phase' = "done"
    /\ UNCHANGED <<kind, approved, started, recorded, steps, crashes, retries>>
    /\ UNCHANGED world

ModelError ==                                     \* 暂时性错误：重试
    /\ phase = "model" /\ retries < MaxRetries
    /\ retries' = retries + 1
    /\ UNCHANGED <<phase, kind, approved, started, recorded, steps, crashes>>
    /\ UNCHANGED world

ModelGiveUp ==                                    \* 重试用完：本轮失败
    /\ phase = "model" /\ retries = MaxRetries
    /\ phase' = "failed"
    /\ UNCHANGED <<kind, approved, started, recorded, steps, crashes, retries>>
    /\ UNCHANGED world

(* ── 人的审批（非确定） ── *)
Grant ==
    /\ phase = "approval" /\ approved' = TRUE /\ phase' = "start"
    /\ UNCHANGED <<kind, started, recorded, steps, crashes, retries>> /\ UNCHANGED world

Deny ==                                           \* 拒绝：本轮结束，交还给人
    /\ phase = "approval" /\ phase' = "done"
    /\ UNCHANGED <<kind, approved, started, recorded, steps, crashes, retries>>
    /\ UNCHANGED world

(* ── harness 执行工具 ── *)
Start ==                                          \* 写 tool_start 并落盘
    /\ phase = "start" /\ started' = TRUE /\ phase' = "exec"
    /\ UNCHANGED <<kind, approved, recorded, steps, crashes, retries>> /\ UNCHANGED world

Gate == IF GateBindsHash THEN work \in verified ELSE verified # {}

Exec ==                                           \* 工具真正执行，外部世界发生变化
    /\ phase = "exec" /\ phase' = "result"
    /\ CASE kind = "pay" ->
              /\ pays' = IF IdempotentPay /\ keyUsed THEN pays ELSE pays + 1
              /\ keyUsed' = TRUE
              /\ UNCHANGED <<work, verified, committed>>
         [] kind = "edit" ->
              /\ work' \in Hashes
              /\ UNCHANGED <<pays, keyUsed, verified, committed>>
         [] kind = "shell" ->                      \* 可以改任何文件；没隔离时还可能顺手提交
              /\ work' \in Hashes
              /\ committed' \in IF ShellConfined THEN {committed}
                                ELSE {committed, committed \cup {work}}
              /\ UNCHANGED <<pays, keyUsed, verified>>
         [] kind = "test" ->                       \* 可能通过，也可能失败
              /\ verified' \in {verified, verified \cup {work}}
              /\ UNCHANGED <<pays, keyUsed, work, committed>>
         [] kind = "commit" ->
              /\ committed' = IF Gate THEN committed \cup {work} ELSE committed
              /\ UNCHANGED <<pays, keyUsed, work, verified>>
         [] kind = "read" -> UNCHANGED world
    /\ UNCHANGED <<kind, approved, started, recorded, steps, crashes, retries>>

Record ==                                         \* 工具结果写进日志，进入下一步
    /\ phase = "result" /\ recorded' = TRUE /\ phase' = "model"
    /\ UNCHANGED <<kind, approved, started, steps, crashes, retries>> /\ UNCHANGED world

(* ── 进程崩溃与恢复 ── *)
Crash ==
    /\ phase \in {"model", "approval", "start", "exec", "result"}
    /\ CrashBound => crashes < MaxCrashes
    /\ crashes' = IF CrashBound THEN crashes + 1 ELSE crashes
    /\ phase' = "crashed"
    /\ approved' = IF ApprovalInJournal THEN approved ELSE FALSE
    /\ UNCHANGED <<kind, started, recorded, steps, retries>> /\ UNCHANGED world

Recover ==                                        \* reliable_agent.Harness._repair
    /\ phase = "crashed"
    /\ phase' =
        IF ~started \/ recorded THEN "model"      \* 没开始（NOT_STARTED）或已有结果
        ELSE IF NaiveRedo THEN "exec"
        ELSE IF Repeatable(kind)
                /\ (ApprovalInJournal => (kind \in Sensitive => approved))
             THEN "exec"                          \* 只读或幂等：带同一个键重做
        ELSE "result"                             \* 结果未知：记 OUTCOME_UNKNOWN
    /\ UNCHANGED <<kind, approved, started, recorded, steps, crashes, retries>>
    /\ UNCHANGED world

Finished ==                                       \* 结束后停在原地
    /\ phase \in {"done", "failed"} /\ UNCHANGED vars

Next == ModelText \/ ModelCall \/ StepLimit \/ ModelError \/ ModelGiveUp
     \/ Grant \/ Deny \/ Start \/ Exec \/ Record \/ Crash \/ Recover \/ Finished

(* 弱公平：只要还有步骤可走，系统不会永远停着不动。
   这里面藏着三个假设：模型调用总会返回或报错，人总会回答审批，崩溃后总会重启。 *)
Spec == Init /\ [][Next]_vars /\ WF_vars(Next)

(* ── 要检查的性质 ── *)
AuthOK     == (phase = "exec" /\ kind \in Sensitive) => approved   \* 要求 A
CommitGate == committed \subseteq verified                         \* 要求 B
AtMostOnce == pays <= 1                                            \* 一次付款调用最多扣一次款
Terminates == <>(phase \in {"done", "failed"})                     \* 要求 C
=============================================================================
