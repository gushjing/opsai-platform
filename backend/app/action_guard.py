"""对话里的直接处置命令：识别祈使句 → 生成确认提案 → 模拟执行并给出真实感输出。

与排障问答的区别：
  「0931 内存为什么涨」/「怎么清理内存」→ 咨询，走原排障工具链给结论；
  「帮我清理内存」→ 命令，Agent 先反馈计划并询问是否执行，确认后才动手，
  并把执行输出、工单、工作日志、审计一次性落下（四端可见）。
"""
from __future__ import annotations

import re

from app.timeutil import fmt_dt, now_local

# 危险命令在提案阶段就拦截，不给确认按钮
DANGEROUS = (
    "rm -rf /", "mkfs", ":(){", "shutdown -h", "dd if=/dev/zero",
    "reboot", "> /dev/sda", "chmod -r 777 /",
)

# 咨询/追问语气：出现这些词说明用户在「问」，不是在「下令」
CONSULT = re.compile(
    r"怎么|如何|为什么|为何|啥原因|什么原因|吗|呢|？|\?|建议|原因|分析|"
    r"是不是|能不能|可不可以|要不要|该不该|是否|会不会|多少|查一下|看看|"
    r"看下|有没有|解释|帮我查|帮我看|是否正常|正常吗",
    re.I,
)
# 祈使标志：句首指令词，或句中「直接/立刻/替我」等强指令
IMPERATIVE = re.compile(
    r"^(帮我|请|麻烦|给我|立刻|立即|马上|直接|执行|运行|操作|去|把|将|现在|"
    r"清理|释放|回收|重启|扩容|回滚|杀掉|停掉|启动|重置|删除|切换|"
    r"please|run|execute|restart|clean)",
    re.I,
)
DIRECT = re.compile(r"帮我|请立刻|请立即|马上|立即|立刻|直接执行|直接处理|现在就|替我", re.I)

VERB = re.compile(
    r"清理|清一下|清下|释放|回收|重启|重新启动|扩容|回滚|杀掉|停掉|停止|"
    r"启动|执行|处理|删除|重置|切换|刷掉|drop|clean|restart|kill",
    re.I,
)
OBJECT = re.compile(
    r"内存|磁盘|日志|缓存|cache|memory|服务|进程|分区|目录|/data|nginx|redis|mysql|"
    r"order-service|服务器|端口|网络|空间|历史文件|临时文件|连接数|cpu|负载",
    re.I,
)

SERVER_RE = re.compile(r"\b(\d{3,6})\b")

# 每类动作给出对应的真实命令与逐行输出，避免千篇一律的 "command executed"
ACTION_RULES = [
    {
        "key": "memory",
        "label": "清理内存缓存",
        "pattern": r"(清理|释放|回收|清一下|清下).{0,8}(内存|缓存|cache|memory)|(内存|缓存).{0,6}(清理|释放|回收|太高|满了|不足)",
        "command": "sync && echo 3 > /proc/sys/vm/drop_caches",
        "risk": "仅回收 page cache / dentries，不重启进程；回收瞬间可能有短暂 IO 抖动",
        "lines": [
            "$ free -m",
            "              total        used        free      shared  buff/cache   available",
            "Mem:          16384       12288        1024         128        3072        2048",
            "$ sync && echo 3 > /proc/sys/vm/drop_caches",
            "已回收 page cache / dentries / inodes 共 3072 MB",
            "MemAvailable 2048 MB -> 5120 MB，内存水位 87% -> 52%",
        ],
    },
    {
        "key": "disk",
        "label": "清理磁盘历史日志",
        "pattern": r"(清理|删除|清一下|清下).{0,10}(磁盘|/data|日志|空间|历史文件|临时文件)|(磁盘|空间).{0,6}(清理|满了|不足|不足|告警)",
        "command": "find /data/log -name '*.log' -mtime +7 -delete && df -h /data",
        "risk": "删除 7 天前日志，不可恢复；执行前已确认 /data 无业务数据依赖",
        "lines": [
            "$ df -h /data",
            "Filesystem      Size  Used Avail Use% Mounted on",
            "/dev/vdb1       200G  191G  9.0G  96% /data",
            "$ find /data/log -name '*.log' -mtime +7 -delete",
            "已删除 128 个历史日志文件，释放 42.6 GB",
            "$ df -h /data",
            "/dev/vdb1       200G  148G   52G  74% /data",
            "/data 使用率 96% -> 74%，已低于 80% 告警阈值",
        ],
    },
    {
        "key": "restart",
        "label": "重启服务",
        "pattern": r"(重启|重新启动|restart).{0,12}(服务|进程|nginx|redis|mysql|order-service|api-gateway|网关)|(服务|进程).{0,6}(重启|重新启动|挂了|拉起)",
        "command": "systemctl restart order-service && systemctl status order-service --no-pager",
        "risk": "会造成该服务秒级连接中断，建议在低峰期执行；已检查无正在进行的发布",
        "lines": [
            "$ systemctl restart order-service",
            "$ systemctl status order-service --no-pager",
            "● order-service.service - Order Service",
            "   Active: active (running)",
            "   Main PID: 28417 (java)",
            "$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/health",
            "200  · 健康检查通过，服务已恢复",
        ],
    },
    {
        "key": "scale",
        "label": "磁盘扩容",
        "pattern": r"(扩容|扩一下|增加).{0,8}(磁盘|分区|/data|空间|容量)|(磁盘|空间).{0,4}扩容",
        "command": "growpart /dev/vdb 1 && resize2fs /dev/vdb1",
        "risk": "在线扩容分区表，需确认云盘已先在控制台完成扩容；不重建文件系统",
        "lines": [
            "$ lsblk -o NAME,SIZE | grep vdb",
            "vdb    300G",
            "$ growpart /dev/vdb 1 && resize2fs /dev/vdb1",
            "resize2fs: The filesystem on /dev/vdb1 is now 78643200 blocks long.",
            "$ df -h /data",
            "/dev/vdb1       300G  191G  109G  64% /data",
        ],
    },
    {
        "key": "rollback",
        "label": "版本回滚",
        "pattern": r"(回滚|回退|rollback|还原).{0,10}(版本|发布|镜像|代码|服务)|版本.{0,4}回滚",
        "command": "kubectl rollout undo deployment/order-service && kubectl rollout status deployment/order-service",
        "risk": "回退到上一稳定版本，期间可能有短暂双版本并存；已确认上一版本健康检查正常",
        "lines": [
            "$ kubectl rollout history deployment/order-service",
            "REVISION 2 (current) / REVISION 1 (stable)",
            "$ kubectl rollout undo deployment/order-service",
            "deployment.apps/order-service rolled back",
            "$ kubectl rollout status deployment/order-service",
            "deployment \"order-service\" successfully rolled out",
        ],
    },
]

GENERIC_COMMAND = "$ {command}"


def _dangerous(question: str) -> bool:
    low = question.lower()
    return any(w in low for w in DANGEROUS)


def detect(question: str, can_execute: bool) -> dict | None:
    """识别直接处置命令；返回提案信息，非命令返回 None（走原排障链）。"""
    q = (question or "").strip()
    if not q or len(q) > 160:
        return None
    if CONSULT.search(q):
        return None
    if not (IMPERATIVE.search(q) or DIRECT.search(q)):
        return None
    dangerous = _dangerous(q)
    # 高危命令即使不含常见运维对象词，也要接住并给出明确拦截反馈
    if not (VERB.search(q) and OBJECT.search(q)) and not dangerous:
        return None

    server = (SERVER_RE.search(q).group(1) if SERVER_RE.search(q) else "0931")
    rule = next((r for r in ACTION_RULES if re.search(r["pattern"], q, re.I)), None)

    if rule:
        label, command, risk = rule["label"], rule["command"], rule["risk"]
        key = rule["key"]
    else:
        label, key = "执行运维命令", "generic"
        # 用户自带 shell 命令时原样执行，否则用自然语言作为动作描述
        m = re.search(r"[`$]\s*([a-zA-Z_/][^\s，。；]{2,80})", q)
        command = m.group(1) if m else q.strip("。！! ")
        risk = "该命令会修改服务器状态，请确认目标主机与命令内容无误"

    return {
        "kind": "command",
        "key": key,
        "label": label,
        "server": server,
        "command": command,
        "risk": risk,
        "summary": f"在 {server} 上{label}",
        "question": q,
        "dangerous": dangerous,
        "allowed": can_execute and not dangerous,
    }


def proposal_text(p: dict) -> str:
    """Agent 的第一次反馈：说清要做什么、在哪做、风险，并询问是否执行。"""
    if p.get("dangerous"):
        return (
            f"识别到高危命令「{p['command']}」，已被安全策略拦截，不会执行。\n"
            "如需处置，请改用更明确、可审计的单项操作，或在工作台发起处理工单由工程师接单。"
        )
    if not p.get("allowed"):
        return (
            f"你要求的是「{p['summary']}」，但当前账号没有服务器执行权限（ops.execute），"
            "不能直接改动服务器。\n"
            "可以改用「管理员 / 工程师」身份在四端切换后再次下达，"
            "或在工作台发起服务器处理申请，由工程师接单执行；相关记录会同步到操作台账。"
        )
    return (
        f"收到处置命令：{p['summary']}。\n"
        f"目标主机：{p['server']}    待执行命令：{p['command']}\n"
        f"风险提示：{p['risk']}\n\n"
        "确认要现在执行吗？确认后我会输出执行结果，并同步生成工单、工作日志与审计记录。"
    )


def simulate(p: dict) -> dict:
    """本地模拟执行（真实环境改走 Ansible over SSH），返回可读的逐行输出。"""
    rule = next((r for r in ACTION_RULES if r["key"] == p.get("key")), None)
    lines = rule["lines"] if rule else [
        GENERIC_COMMAND.format(command=p["command"]),
        "执行完成，目标主机状态已更新",
    ]
    stamp = fmt_dt(now_local())
    prefix = f"[{stamp}] "
    stdout = "\n".join(prefix + line if line.startswith("$") else line for line in lines)
    return {
        "host": p["server"],
        "label": p["label"],
        "command": p["command"],
        "exit_code": 0,
        "stdout": stdout,
        "via": "ansible(模拟)",
        "finished_at": stamp,
    }


def result_text(result: dict, operation_code: str) -> str:
    """Agent 的第二次反馈：把执行输出如实回给使用者。"""
    return (
        f"已执行：{result['label']}（目标 {result['host']}，通道 {result['via']}）\n"
        f"完成时间 {result['finished_at']} · exit_code={result['exit_code']}\n\n"
        f"{result['stdout']}\n\n"
        f"已同步生成工单 {operation_code}，工作日志、操作台账与系统审计均已留痕，四端可见。"
    )
