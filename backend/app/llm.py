"""大语言模型适配层：通义千问 Qwen（DashScope OpenAI 兼容模式）。

统一接口隔离供应商以满足「模型热切换」；未配置 API-Key 或调用失败时降级为
「意图感知的规则推理」——按提问焦点与真实证据数值作答，而不是套用同一份模板。
"""
import json
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config import settings

SYSTEM_PROMPT = """你是资深运维排障专家，只处理服务器故障排障类问题。根据提供的运维证据，输出严格的 JSON（不要任何额外文字）：
{"problem":"针对用户提问焦点的一句话结论","location":"具体位置（主机/分区/服务/调用链节点）","evidence":["带实际数值或日志原文的关键证据"],"suggestions":["可执行的建议措施"],"confidence":0.0到1.0}
严格要求：
1. 结论必须由给定证据支撑，逐条引用其中的实际数值、日志原文或 span 名称，不得臆测、不得套用通用模板。
2. 用户只问某个指标就只答该指标；只问日志就只答日志；只问链路就只答链路，不要扩展成无关根因。
3. 若问题未指定服务器编号，注明按当前默认数据源作答。
4. 证据不足时 confidence 低于 0.5，并说明还需补充什么数据。
5. 不属于运维排障领域的问题不要回答（由上层守卫拦截）。"""


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rule_based(question: str, evidence: dict, intent: str = "diagnose",
                focus: str | None = None) -> dict:
    """按意图分派到不同推理分支，每条证据都来自本次工具实际拉回的数据。"""
    metrics = evidence.get("metrics") or {}
    logs = evidence.get("logs") or []
    trace = evidence.get("trace") or {}
    info = evidence.get("server_info") or {}
    server = evidence.get("server") or "当前服务器"
    explicit = evidence.get("server_explicit", True)
    note = "" if explicit else "（问题未指定服务器编号，按当前接入的默认数据源 0931 作答）"

    errors = [x for x in logs if str(x.get("level", "")).upper() in ("ERROR", "FATAL")]
    disk = _num(metrics.get("disk_usage"))
    cpu = _num(metrics.get("cpu_usage"))
    mem = _num(metrics.get("memory_usage"))
    io = _num(metrics.get("disk_io_wait"))
    e5 = _num(metrics.get("http_5xx_rate"))
    load = _num(metrics.get("load_1m"))
    mount = metrics.get("mount") or "/data"
    disk_full = any("space left" in str(x.get("message", "")).lower() for x in errors)
    spans = trace.get("spans") or []
    hint = trace.get("root_cause_hint") or ""
    hidden_logs = bool(evidence.get("raw_logs_hidden"))
    hidden_trace = bool(evidence.get("raw_trace_hidden"))
    src = metrics.get("source", "mock")

    # ---------- 指标查询：只答用户问的那一项 ----------
    if intent == "metrics":
        metric_lines = [
            f"CPU 使用率 {cpu}%、1 分钟负载 {load}",
            f"内存使用率 {mem}%",
            f"{mount} 分区磁盘使用率 {disk}%（告警阈值 85%）",
            f"磁盘 iowait {io}%",
            f"HTTP 5xx 比例 {e5}%",
        ]
        focus_map = {
            "disk": (f"{server} {mount} 分区磁盘使用率 {disk}%，iowait {io}%，"
                     f"{'已超过 85% 告警阈值' if disk > 85 else '处于正常区间'}",
                     [metric_lines[2], metric_lines[3], f"数据源 {src}"]),
            "cpu": (f"{server} CPU 使用率 {cpu}%，1 分钟负载 {load}，"
                    f"{'负载偏高' if load > 4 else '处于正常区间'}",
                    [metric_lines[0], f"数据源 {src}"]),
            "memory": (f"{server} 内存使用率 {mem}%，"
                       f"{'偏高' if mem > 85 else '处于正常区间'}",
                       [metric_lines[1], f"数据源 {src}"]),
            "io": (f"{server} 磁盘 iowait {io}%，"
                   f"{'写入存在阻塞' if io > 20 else 'I/O 正常'}",
                   [metric_lines[3], metric_lines[2], f"数据源 {src}"]),
            "http": (f"{server} HTTP 5xx 比例 {e5}%，"
                     f"{'接口错误率明显升高' if e5 > 1 else '接口错误率正常'}",
                     [metric_lines[4], f"数据源 {src}"]),
            "load": (f"{server} 1 分钟负载 {load}，CPU 使用率 {cpu}%",
                     [metric_lines[0], f"数据源 {src}"]),
        }
        if focus in focus_map:
            problem, ev = focus_map[focus]
        else:
            problem = f"{server} 当前各项监控指标：CPU {cpu}%、内存 {mem}%、磁盘 {disk}%、iowait {io}%、5xx {e5}%"
            ev = metric_lines + [f"数据源 {src}"]
        result = {
            "problem": problem + note,
            "location": f"{server} · 指标数据源 {src}",
            "evidence": ev,
            "suggestions": (["清理过期日志与临时文件释放磁盘空间", "配置磁盘水位 85% 告警与日志轮转"]
                            if disk > 85 else ["当前指标正常，保持现有监控与告警阈值即可"]),
            "confidence": 0.95,
        }

    # ---------- 日志检索：只列真实日志条目 ----------
    elif intent == "logs":
        if hidden_logs or not logs:
            result = {
                "problem": f"{server} 原始日志检索：当前角色无查看权限，仅可见概览",
                "location": f"{server} · 日志数据源权限受限",
                "evidence": ["当前角色不具备 logs.view 权限，未返回原始日志条目"],
                "suggestions": ["联系管理员或工程师查看原始日志", "可改用「为什么出问题」获取概览结论"],
                "confidence": 0.6,
            }
        else:
            lines = [
                f"[{x.get('timestamp', '')[:19]}] {x.get('level')} "
                f"{x.get('service')}: {x.get('message')}"
                for x in logs[:8]
            ]
            svc = sorted({str(x.get("service")) for x in errors}) or ["order-service"]
            result = {
                "problem": (f"{server} 近 30 分钟检索到 {len(logs)} 条日志，"
                            f"其中 ERROR/FATAL {len(errors)} 条，集中在 {', '.join(svc)}"),
                "location": f"{server} · {', '.join(svc)} 日志写入路径",
                "evidence": lines or ["该时间窗口无日志"],
                "suggestions": (["核查写入失败对应的磁盘空间与挂载点", "为该服务配置 ERROR 日志实时告警"]
                                if errors else ["该时段无错误日志，可扩大时间窗口再检索"]),
                "confidence": 0.9 if errors else 0.7,
            }

    # ---------- 链路追踪：定位到具体 span ----------
    elif intent == "trace":
        if hidden_trace or not spans:
            result = {
                "problem": f"{server} 链路追踪：当前角色无查看权限或该主机暂无异常调用链",
                "location": f"{server} · 链路数据源权限受限",
                "evidence": ["当前角色不具备 trace.view 权限，或该主机调用链均为健康状态"],
                "suggestions": ["联系工程师查看完整调用链", "改用「为什么出问题」获取概览结论"],
                "confidence": 0.6,
            }
        else:
            bad = [s for s in spans if str(s.get("status", "")).upper() == "ERROR"]
            # 优先定位带明确错误信息的下游节点（根因），而不是单纯耗时最长的入口节点
            candidates = [s for s in spans if s.get("error")] or bad or spans
            worst = max(candidates, key=lambda s: _num(s.get("duration_ms")))
            lines = [
                f"{s.get('service')} · {s.get('operation')} · {s.get('duration_ms')} ms · "
                f"{s.get('status')}{' · ' + str(s.get('error')) if s.get('error') else ''}"
                for s in spans
            ]
            result = {
                "problem": (f"{server} 调用链 {trace.get('trace_id')} 中 "
                            f"{worst.get('service')} → {worst.get('operation')} "
                            f"耗时 {worst.get('duration_ms')} ms 并抛出异常，为根因节点；"
                            f"共 {len(bad)} 个 ERROR span"),
                "location": f"{server} · {worst.get('service')} → {worst.get('operation')}",
                "evidence": lines + ([f"根因提示：{hint}"] if hint else []),
                "suggestions": ["优先排查该节点的下游存储写入能力", "为该 span 配置耗时告警"],
                "confidence": 0.9,
            }

    # ---------- 处置建议：基于根因给可执行步骤 ----------
    elif intent == "suggestion":
        if disk > 90 or disk_full:
            result = {
                "problem": f"针对 {server} 的处置建议：先释放 {mount} 分区空间（当前 {disk}%）再恢复服务",
                "location": f"{server} {mount} 分区 → 写入相关服务",
                "evidence": [f"磁盘使用率 {disk}%（阈值 85%）", f"iowait {io}%，写入阻塞",
                             f"5xx 比例 {e5}%"] +
                            ([f"链路根因：{hint}"] if hint else []),
                "suggestions": [
                    f"先清理 {mount} 下历史日志、临时文件与过期备份，把水位降到 80% 以下",
                    "再重启写入受影响的服务（如 order-service）恢复接口",
                    "配置日志轮转与 85% 磁盘水位告警，避免复发",
                ],
                "confidence": 0.88,
            }
        else:
            result = {
                "problem": f"{server} 当前无异常证据（磁盘 {disk}%、CPU {cpu}%、5xx {e5}%），不建议盲目执行处置动作",
                "location": f"{server}（未定位到需处置的故障点）",
                "evidence": [f"磁盘 {disk}%、CPU {cpu}%、内存 {mem}%",
                             "未捕获 ERROR 日志与异常 span"],
                "suggestions": ["先确认故障发生的具体时间点与现象", "补充该时间窗口的指标与日志后再给出处置方案"],
                "confidence": 0.4,
            }

    # ---------- 服务器基础信息 ----------
    elif intent == "server_info":
        result = {
            "problem": f"{server} 基础信息：IP {info.get('ip', '未采集')}，当前状态 {info.get('status', '未知')}",
            "location": f"{server} · 资产与健康状态",
            "evidence": [f"IP {info.get('ip')}", f"状态 {info.get('status')}",
                         f"CPU {info.get('cpu_usage')}%", f"磁盘 {info.get('disk_usage')}%",
                         f"数据源 {info.get('source', src)}"],
            "suggestions": ["如需完整资产信息，接入 CMDB 数据源"],
            "confidence": 0.85,
        }

    # ---------- 根因诊断：证据驱动，正常主机不套用故障模板 ----------
    else:
        if disk > 90 or disk_full:
            hints = [
                f"{mount} 分区磁盘使用率 {disk}%，超过 85% 告警阈值（数据源 {src}）",
                f"磁盘 iowait {io}%，写入阻塞；HTTP 5xx 比例 {e5}%",
            ]
            if errors:
                hints.append(f"近 30 分钟捕获 {len(errors)} 条 ERROR：{str(errors[0].get('message'))[:90]}")
            if hint:
                hints.append(f"调用链定位：{hint}")
            if hidden_logs:
                hints.append("当前角色无原始日志查看权限，结论基于指标与链路概览")
            result = {
                "problem": f"{server} {mount} 分区磁盘空间耗尽，导致服务写入失败、接口 5xx 升高",
                "location": f"{server} {mount} 分区 → order-service / db.insert 写入节点",
                "evidence": hints,
                "suggestions": [
                    f"清理 {mount} 下的历史日志、临时文件与过期备份，释放磁盘空间",
                    "为 /data 配置日志轮转与磁盘水位告警（阈值 85%）",
                    "短期可重启 order-service 恢复写入，但需先释放空间避免复发",
                ],
                "confidence": 0.93 if (errors or hint) else 0.78,
            }
        else:
            result = {
                "problem": f"未在 {server} 发现明确的单一根因，当前指标与日志处于正常范围{note}",
                "location": f"{server}（未定位到具体故障点）",
                "evidence": [
                    f"CPU {cpu}%、内存 {mem}%、磁盘 {disk}%、iowait {io}%（数据源 {src}）",
                    f"HTTP 5xx 比例 {e5}%、1 分钟负载 {load}",
                    f"该时段 ERROR 日志 {len(errors)} 条" if logs else "未返回原始日志数据",
                    "调用链无 ERROR span" if not spans else f"调用链 {len(spans)} 个 span，均正常",
                ],
                "suggestions": ["补充该服务器更长时间窗口的指标与日志", "确认故障发生的具体时间点后再分析"],
                "confidence": 0.35,
            }

    result["intent"] = intent
    result["focus"] = focus
    result["server"] = server
    result["server_explicit"] = explicit
    result["source"] = src
    return result


def _extract_json(text: str) -> dict | None:
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) and "problem" in data else None
    except json.JSONDecodeError:
        return None


async def analyze(question: str, evidence: dict, intent: str = "diagnose",
                  focus: str | None = None) -> dict:
    """返回结构化排障结果：问题描述、故障位置、证据、建议措施。"""
    if not settings.dashscope_api_key:
        result = _rule_based(question, evidence, intent, focus)
        result["mode"] = "rule-fallback"
        result["model"] = "未配置 DASHSCOPE_API_KEY"
        return result

    try:
        model = ChatOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.qwen_base_url,
            model=settings.qwen_model,
            temperature=0.1,
            timeout=30,
        )
        payload = json.dumps(
            {"question": question, "intent": intent, "focus": focus,
             "evidence": evidence}, ensure_ascii=False
        )
        resp = await model.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=payload)]
        )
        parsed = _extract_json(str(resp.content))
        if parsed:
            parsed["mode"] = "qwen"
            parsed["model"] = settings.qwen_model
            parsed["intent"] = intent
            parsed["focus"] = focus
            parsed["server"] = evidence.get("server")
            parsed["server_explicit"] = evidence.get("server_explicit", True)
            return parsed
        raise ValueError("LLM 返回内容不是预期 JSON")
    except Exception as exc:
        result = _rule_based(question, evidence, intent, focus)
        result["mode"] = "rule-fallback"
        result["model"] = f"Qwen 调用失败，已降级：{type(exc).__name__}"
        return result
