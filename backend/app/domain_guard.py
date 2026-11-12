"""领域边界守卫：只受理服务器排障类专业性问句。

无法确认属于运维专业范围时，一律返回固定拒答文案（不臆测、不套模板），
避免系统对闲聊/通用问题给出看似专业实则无关的排障结论。
"""
import re

OUT_OF_SCOPE_REPLY = (
    "系统目前不支持该类对话。\n"
    "我专注于服务器故障排障，可回答监控指标查询、日志检索、调用链定位、"
    "故障根因分析与处置建议等专业问题，例如：「0931 服务器为什么出现问题？」"
)

SERVER_RE = re.compile(r"\b(\d{3,6})\b")

# 明显领域外的话题：命中且不含运维术语时直接拒答
OFF_TOPIC = [
    r"天气|气温|下雨|下雪|台风",
    r"讲(个|一)?(笑话|故事)|写(一首|篇|段|个)?(诗|代码|程序|文案|作文|邮件)|翻译|译成|润色",
    r"你是谁|你叫什么|你多大|你能做什么|你有什么功能|介绍一下你自己|你是(什么|谁)模型",
    r"股票|基金|彩票|菜谱|做饭|电影|音乐|游戏|旅游|星座|算命|情感|恋爱",
    r"今天(星期几|几号|什么日子)|现在几点|几点了|日期",
    r"计算|等于多少|帮我算|1\s*[+\-*/]\s*1",
    r"^(谢谢|感谢|多谢|拜拜|再见|好的|好的呀|嗯+|哦+|ok|okay|收到|哈哈哈)[\s!！?？。~，,]*$",
    r"^(你好|您好|hi|hello|hey|哈喽|在吗|在么|早上好|中午好|晚上好|hiya)[\s!！?？。~，,]*$",
]

# 运维领域术语：判定「专业性」的必要条件之一
DOMAIN_TERMS = [
    r"服务器|主机|实例|节点|容器|pod|集群|机房|磁盘阵列",
    r"cpu|内存|磁盘|分区|/data|iowait|负载|load|使用率|水位|带宽|qps|tps|5xx|4xx|错误率|延迟|响应时间",
    r"日志|log\b|error|warn|fatal|堆栈|报错|异常|告警|报警|监控|指标|prometheus|grafana|elk",
    r"链路|调用链|trace|span|超时|耗时|变慢|很慢|响应慢|接口慢|抖动",
    r"故障|排查|根因|原因|为什么|出问题|挂了|宕机|不可用|崩|重启|恢复|处理|修复|清理|扩容|回滚|处置|建议|措施|怎么办|怎么处理|定位",
    r"服务|进程|端口|数据库|mysql|redis|nginx|order-service|api-gateway|接口|网关",
    r"正常吗|健康度|状态|有没有问题|是否异常|是否可用",
]

INTENT_RULES = [
    ("trace", r"调用链|链路|trace|span|哪个节点|慢在哪|哪里慢|最慢|耗时"),
    ("logs", r"日志|log\b|error|warn|fatal|堆栈|报错信息|错误码|报错内容|错误日志"),
    ("suggestion", r"怎么处理|怎么办|如何解决|如何处置|建议|措施|修复|清理|重启|恢复|扩容|回滚|方案|处置"),
    ("metrics", r"cpu|内存|磁盘|分区|iowait|负载|使用率|水位|qps|5xx|4xx|错误率|指标|健康度|多少|是否正常|正常吗|状态|采样"),
    ("diagnose", r"为什么|啥原因|什么原因|故障|排查|根因|出问题|挂了|宕机|不可用|崩|异常|报错|超时|变慢|很慢|卡|定位|分析"),
    ("server_info", r"服务器信息|基本信息|ip地址|配置|归属|负责人|版本"),
]

FOCUS_RULES = [
    ("disk", r"磁盘|分区|/data|空间|disk"),
    ("cpu", r"cpu|处理器"),
    ("memory", r"内存|memory"),
    ("io", r"iowait|磁盘io|io\b|读写"),
    ("http", r"5xx|4xx|错误率|接口|请求|http"),
    ("load", r"负载|load"),
]

FOLLOWUP = r"^(那|再|还有|继续|具体|详细|哪个|哪台|依据|证据|怎么|为什么|然后|接着|以及|上面|刚才|这条|它|这个)"


def _hit(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


def classify(question: str, history: list[dict] | None = None) -> dict:
    """返回 {in_scope, intent, server, has_server, focus, is_followup, reason}。"""
    q = (question or "").strip()
    low = q.lower()
    m = SERVER_RE.search(q)
    server = m.group(1) if m else None
    has_domain = _hit(DOMAIN_TERMS, low)
    off_topic = _hit(OFF_TOPIC, low)

    # 1) 明确领域外且无运维术语 → 拒答
    if off_topic and not has_domain:
        return {"in_scope": False, "intent": "out_of_scope", "server": None,
                "has_server": False, "focus": None, "is_followup": False,
                "reason": "命中领域外话题且不含运维术语"}

    # 2) 追问：承接上一轮排障上下文
    history = history or []
    # 只认真正的排障结论（含「【问题】」结构化标记），拒答文案不构成上下文
    has_pro_context = any(
        h.get("role") == "assistant" and "【问题】" in h.get("content", "")
        for h in history
    )
    is_followup = bool(history) and has_pro_context and (
        re.match(FOLLOWUP, q) is not None or len(q) <= 12
    )
    if is_followup and not has_domain:
        return {"in_scope": True, "intent": "diagnose", "server": None,
                "has_server": False, "focus": None, "is_followup": True,
                "reason": "承接上一轮排障上下文的追问"}

    # 3) 无运维术语、无服务器编号 → 一律拒答（宁可拒答，不套模板）
    if not has_domain and not server:
        return {"in_scope": False, "intent": "out_of_scope", "server": None,
                "has_server": False, "focus": None, "is_followup": False,
                "reason": "未识别到运维专业意图"}

    # 4) 专业性问句：识别意图与关注点
    intent = "diagnose"
    for name, pattern in INTENT_RULES:
        if re.search(pattern, low, re.I):
            intent = name
            break
    focus = None
    for name, pattern in FOCUS_RULES:
        if re.search(pattern, low, re.I):
            focus = name
            break

    return {
        "in_scope": True, "intent": intent, "server": server,
        "has_server": server is not None, "focus": focus,
        "is_followup": is_followup, "reason": f"专业意图 {intent}",
    }
