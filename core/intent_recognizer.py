"""IT-Copilot 意图识别：PydanticAI 结构化输出、向量相似度和规则三路投票，并做置信度校准。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput
from pydantic_ai.settings import ModelSettings

from core.intent_calibration import calibrate, load_temperatures
from core.intent_embedding import IntentEmbedding
from core.llm import build_model

logger = logging.getLogger(__name__)


class IntentCategory(str, Enum):
    DEVICE_FAILURE = "device_failure"
    DEVICE_PROVISIONING = "device_provisioning"
    PASSWORD_RESET = "password_reset"
    ACCOUNT_UNLOCK = "account_unlock"
    MFA_ISSUE = "mfa_issue"
    PERMISSION_REQUEST = "permission_request"
    ACCESS_REVIEW = "access_review"
    VPN_ISSUE = "vpn_issue"
    WIFI_ISSUE = "wifi_issue"
    NETWORK_ISSUE = "network_issue"
    SOFTWARE_INSTALL = "software_install"
    SOFTWARE_LICENSE = "software_license"
    EMAIL_COLLAB_ISSUE = "email_collab_issue"
    HOWTO = "howto"
    TICKET_CREATE = "ticket_create"
    TICKET_STATUS = "ticket_status"
    TICKET_CANCEL = "ticket_cancel"
    HUMAN_HANDOFF = "human_handoff"
    GREETING = "greeting"
    CHITCHAT = "chitchat"
    OUT_OF_SCOPE = "out_of_scope"
    OTHER = "other"


class UrgencyLevel(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


I = IntentCategory
INTENT_GROUPS: Dict[IntentCategory, str] = {
    I.DEVICE_FAILURE: "endpoint", I.DEVICE_PROVISIONING: "endpoint",
    I.PASSWORD_RESET: "access", I.ACCOUNT_UNLOCK: "access", I.MFA_ISSUE: "access", I.PERMISSION_REQUEST: "access", I.ACCESS_REVIEW: "access",
    I.VPN_ISSUE: "network", I.WIFI_ISSUE: "network", I.NETWORK_ISSUE: "network",
    I.SOFTWARE_INSTALL: "software", I.SOFTWARE_LICENSE: "software", I.EMAIL_COLLAB_ISSUE: "software",
    I.HOWTO: "knowledge",
    I.TICKET_CREATE: "ticket", I.TICKET_STATUS: "ticket", I.TICKET_CANCEL: "ticket",
    I.HUMAN_HANDOFF: "escalation",
    I.GREETING: "general", I.CHITCHAT: "general", I.OUT_OF_SCOPE: "general", I.OTHER: "general",
}

# 向量匹配的意图模板。
TEMPLATES: Dict[IntentCategory, List[str]] = {
    I.DEVICE_FAILURE: ["电脑蓝屏", "显示器黑屏", "键盘无法使用"],
    I.DEVICE_PROVISIONING: ["申请新电脑", "入职需要设备", "领取办公电脑"],
    I.PASSWORD_RESET: ["重置密码", "忘记域账号密码", "密码过期"],
    I.ACCOUNT_UNLOCK: ["账号被锁定", "解锁企业账号"],
    I.MFA_ISSUE: ["MFA 验证失败", "收不到二次验证", "重新绑定验证器"],
    I.PERMISSION_REQUEST: ["申请系统权限", "开通应用访问权限"],
    I.ACCESS_REVIEW: ["检查我的权限", "权限复核"],
    I.VPN_ISSUE: ["VPN 连不上", "VPN 连接超时"],
    I.WIFI_ISSUE: ["办公 WiFi 断开", "无线网络连不上"],
    I.NETWORK_ISSUE: ["网络很慢", "无法访问内网"],
    I.SOFTWARE_INSTALL: ["安装办公软件", "安装开发工具"],
    I.SOFTWARE_LICENSE: ["申请软件许可证", "许可证过期"],
    I.EMAIL_COLLAB_ISSUE: ["邮箱收不到邮件", "会议协作工具异常"],
    I.TICKET_CREATE: ["帮我创建 IT 工单", "提交服务台请求"],
    I.TICKET_STATUS: ["查询工单状态", "我的工单处理到哪了"],
    I.TICKET_CANCEL: ["取消我的工单", "撤回服务台申请"],
    I.HUMAN_HANDOFF: ["转人工", "升级给 IT 服务台"],
    I.GREETING: ["你好", "有人可以帮忙吗"],
    I.CHITCHAT: ["谢谢", "辛苦了"],
}

# 规则匹配关键词：命中数最多的意图胜出。
KEYWORDS: Dict[IntentCategory, List[str]] = {
    I.VPN_ISSUE: ["vpn", "虚拟专网"],
    I.PASSWORD_RESET: ["重置密码", "忘记密码", "密码过期", "域账号密码"],
    I.ACCOUNT_UNLOCK: ["账号被锁", "账户被锁", "解锁账号", "解锁企业账号"],
    I.MFA_ISSUE: ["mfa", "二次验证", "验证码收不到", "验证器"],
    I.PERMISSION_REQUEST: ["申请权限", "开通权限", "访问权限", "权限"],
    I.ACCESS_REVIEW: ["检查一下我的系统权限", "检查权限", "权限复核"],
    I.DEVICE_PROVISIONING: ["申请电脑", "新电脑", "领设备", "设备申领", "办公设备"],
    I.DEVICE_FAILURE: ["蓝屏", "黑屏", "电脑坏", "设备故障", "键盘失灵", "键盘无法使用"],
    I.WIFI_ISSUE: ["wifi", "wi-fi", "无线网", "无线网络"],
    I.NETWORK_ISSUE: ["网络慢", "内网", "网络故障"],
    I.SOFTWARE_INSTALL: ["安装软件", "安装应用", "装软件", "安装开发工具", "安装办公软件"],
    I.SOFTWARE_LICENSE: ["许可证", "license", "授权过期"],
    I.EMAIL_COLLAB_ISSUE: ["邮箱", "邮件", "会议协作", "协作工具"],
    I.TICKET_CREATE: ["创建工单", "提交工单", "服务台请求", "报修"],
    I.TICKET_STATUS: ["工单状态", "工单进度", "处理到哪"],
    I.TICKET_CANCEL: ["取消工单", "撤回工单"],
    I.HUMAN_HANDOFF: ["转人工", "升级处理", "服务台人工", "升级给 IT"],
    I.HOWTO: ["怎么", "如何", "教程", "配置步骤", "使用教程"],
    I.OUT_OF_SCOPE: ["写一首诗", "天气怎么样", "股票", "讲个笑话"],
    I.GREETING: ["你好", "您好", "hello", "hi", "嗨", "早上好"],
    I.CHITCHAT: ["你是谁", "你叫什么", "你能做什么", "谢谢", "辛苦了", "再见"],
}

# 写操作类意图的必填槽位；缺失时先澄清。
REQUIRED_SLOTS: Dict[IntentCategory, tuple] = {
    I.PERMISSION_REQUEST: ("employee_id", "system"),
    I.DEVICE_PROVISIONING: ("employee_id", "device_id"),
    I.PASSWORD_RESET: ("employee_id",),
    I.ACCOUNT_UNLOCK: ("employee_id",),
    I.TICKET_CREATE: ("employee_id",),
    I.TICKET_STATUS: ("ticket_id",),
}

# 追问继承上一轮意图组时，用于代表该组的意图。
REPRESENTATIVE_INTENTS: Dict[str, IntentCategory] = {
    "network": I.NETWORK_ISSUE, "access": I.PERMISSION_REQUEST, "endpoint": I.DEVICE_FAILURE,
    "ticket": I.TICKET_STATUS, "software": I.SOFTWARE_INSTALL,
}

ENTITY_PATTERNS: Dict[str, str] = {
    "employee_id": r"(?:员工编号|员工号|工号|employee[_ -]?id)\s*(?:是|为|:|：)?\s*([A-Za-z0-9_-]{3,32})",
    "device_id": r"(?:设备号|资产号|asset[_ -]?tag|device[_ -]?id)\s*[:：]?\s*([A-Za-z0-9_-]{3,32})",
    "hostname": r"(?:主机名|hostname)\s*[:：]?\s*([A-Za-z0-9_.-]{2,64})",
    "ticket_id": r"(?:工单号|ticket[_ -]?id)\s*[:：]?\s*([A-Za-z0-9_-]{3,32})",
    "error_code": r"\b((?:HTTP[-_ ]?)?[45]\d\d|VPN[-_]?\d{2,4}|MFA[-_]?\d{2,4}|[A-Z]{2,12}[-_]\d{2,5})\b",
    "vpn_type": r"\b(SSL VPN|IPSec|WireGuard|OpenVPN)\b",
}
# 部门/地点允许中文且不去重，保持与历史输出一致。
FREE_TEXT_ENTITY_PATTERNS: Dict[str, str] = {
    "department": r"(?:部门|department)\s*[:：]?\s*([一-鿿A-Za-z0-9_-]{2,40})",
    "location": r"(?:地点|办公地点|location)\s*[:：]?\s*([一-鿿A-Za-z0-9_-]{2,40})",
}

FOLLOWUP_CUES = re.compile(r"还是不行|仍然|依旧|那个|这个|它|第二个|上面|刚才|继续|然后呢", re.I)
# 这些组不代表具体 IT 领域；追问句命中它们时应继承上一轮的意图组。
NON_DOMAIN_GROUPS = {"general", "knowledge"}
SOCIAL_INTENTS = {I.GREETING, I.CHITCHAT, I.OUT_OF_SCOPE}
VOTE_WEIGHTS = (("llm", .55), ("embedding", .25), ("pattern", .2))
del I


class IntentEntitiesOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    employee_id: List[str] = []
    device_id: List[str] = []
    ticket_id: List[str] = []
    error_code: List[str] = []
    system: List[str] = []
    role: List[str] = []
    department: List[str] = []
    location: List[str] = []


class IntentClassification(BaseModel):
    """classify_it_request 的结构化输出契约。"""
    model_config = ConfigDict(extra="ignore")
    intent: IntentCategory
    confidence: Optional[float] = Field(None, ge=0, le=1, description="必填，禁止为 0、null 或省略；greeting/chitchat/out_of_scope 建议 0.80-0.95")
    urgency: UrgencyLevel = UrgencyLevel.NORMAL
    reasoning: str = ""
    entities: IntentEntitiesOutput = Field(default_factory=IntentEntitiesOutput)


@dataclass
class IntentResult:
    intent: IntentCategory
    confidence: float
    intent_group: str
    source_scores: Dict[str, float] = field(default_factory=dict)
    urgency: UrgencyLevel = UrgencyLevel.NORMAL
    entities: Dict[str, List[str]] = field(default_factory=dict)
    reasoning: str = ""
    embedding_source: str = "local-ngram"
    clarification_needed: bool = False
    missing_slots: List[str] = field(default_factory=list)
    inherited_from_previous: bool = False
    llm_used: bool = False
    recognition_source: str = "fallback"


def _fresh_metrics() -> Dict[str, Any]:
    return {"llm_calls": 0, "llm_success": 0, "fallback_calls": 0, "fallback_disabled": 0, "fallback_errors": 0, "last_error": None}


def _fallback_vote(source: str) -> Dict[str, Any]:
    # 不携带 urgency：LLM 不可用时紧急度由本地规则 _urgency 判断。
    return {"intent": IntentCategory.OTHER, "confidence": 0.5, "entities": {}, "reasoning": "local fallback", "_source": source}


class IntentRecognizer:
    def __init__(self, api_key: str, base_url: Optional[str] = None, model: str = ""):
        default_calibration = os.path.join(os.path.dirname(__file__), "../evaluation/config/intent_calibration.json")
        self.llm = build_model(api_key, base_url, model)
        self.model = model
        self._classifier = Agent(output_type=ToolOutput(IntentClassification, name="classify_it_request", description="分类 IT 意图并抽取非敏感实体。"), name="intent_classifier")
        self._embedding = IntentEmbedding()
        self._cache: Dict[str, IntentResult] = {}
        self._cache_limit = max(100, int(os.getenv("IT_INTENT_CACHE_SIZE", "2048")))
        self._temperatures = load_temperatures(os.getenv("IT_INTENT_CALIBRATION_PATH", default_calibration))
        self._clarify_threshold = float(os.getenv("IT_INTENT_CLARIFY_THRESHOLD", "0.60"))
        self._metrics = _fresh_metrics()

    def metrics(self) -> Dict[str, Any]:
        return dict(self._metrics)

    def reset_metrics(self) -> None:
        self._metrics = _fresh_metrics()

    async def recognize(self, message: str, history: Optional[List[Dict[str, str]]] = None, previous_intent_group: Optional[str] = None) -> IntentResult:
        key = self._cache_key(message, history, previous_intent_group)
        if key in self._cache:
            return self._cache[key]

        pattern = self._pattern(message)
        embedding = await self._embedding_match(message)
        if os.getenv("IT_INTENT_LLM_ENABLED", "true").lower() in {"1", "true", "yes"}:
            llm = await self._llm(message, history)
        else:
            self._metrics["fallback_calls"] += 1
            self._metrics["fallback_disabled"] += 1
            llm = _fallback_vote("fallback_disabled")

        intent, confidence, scores = self._vote(llm, embedding, pattern)
        confidence = calibrate(confidence, self._temperatures.get(INTENT_GROUPS.get(intent, "general"), 1.0))
        inherited_intent = self._inherited_intent(message, pattern["intent"], previous_intent_group)
        if inherited_intent is not None:
            intent, confidence = inherited_intent, max(confidence, .6)
        group = INTENT_GROUPS.get(intent, "general")

        entities = self._merge_entities(self._extract_entities(message), llm.get("entities", {}))
        missing = self._missing_slots(intent, entities)
        low_confidence = confidence < self._clarify_threshold or scores.get("group_agreement", 1.0) < 0.34
        if low_confidence and not missing:
            missing = ["请补充具体 IT 问题或上下文"]

        result = IntentResult(
            intent=intent,
            confidence=confidence,
            intent_group=group,
            source_scores=scores,
            urgency=llm.get("urgency") or self._urgency(message, intent),
            entities=entities,
            reasoning=str(llm.get("reasoning", ""))[:500],
            embedding_source=embedding.get("source", "local-ngram"),
            clarification_needed=bool(missing),
            missing_slots=missing,
            inherited_from_previous=inherited_intent is not None,
            llm_used=bool(llm.get("_llm_used")),
            recognition_source=llm.get("_source", "fallback"),
        )
        self._cache[key] = result
        if len(self._cache) > self._cache_limit:
            self._cache.pop(next(iter(self._cache)))
        return result

    @staticmethod
    def _inherited_intent(message: str, pattern_intent: IntentCategory, previous_group: Optional[str]) -> Optional[IntentCategory]:
        """追问句（“还是不行”“那个权限呢”）沿用上一轮的意图组。

        规则命中的若是具体领域且与上一轮同组，保留更具体的规则意图；
        命中非领域意图（闲聊、HOWTO、未知）时取上一轮组的代表意图；
        命中其他领域时视为换话题，不继承。
        """
        if not previous_group or not FOLLOWUP_CUES.search(message):
            return None
        pattern_group = INTENT_GROUPS.get(pattern_intent, "general")
        if pattern_group == previous_group:
            return pattern_intent
        if pattern_group in NON_DOMAIN_GROUPS:
            return REPRESENTATIVE_INTENTS.get(previous_group, IntentCategory.OTHER)
        return None

    @staticmethod
    def _representative_intent(group: str) -> IntentCategory:
        return REPRESENTATIVE_INTENTS.get(group, IntentCategory.OTHER)

    @staticmethod
    def _missing_slots(intent: IntentCategory, entities: Dict[str, List[str]]) -> List[str]:
        return [key for key in REQUIRED_SLOTS.get(intent, ()) if not entities.get(key)]

    async def _llm(self, message: str, history: Optional[List[Dict[str, str]]]) -> Dict[str, Any]:
        prompt = f"""你是企业 IT 服务台分类器。必须调用 classify_it_request，禁止直接输出自然语言。
无论用户是在提 IT 问题，还是打招呼（greeting）、闲聊（chitchat）或超出范围（out_of_scope），都必须完整填写所有字段。
confidence 是 0 到 1 的数字且为必填：明确的 IT 意图通常填 0.80-0.95；greeting/chitchat/out_of_scope 也必须填合理值，建议 0.80-0.95；绝对不能留空、填 null 或填 0。
用户消息：{message}
历史：{json.dumps(history[-4:] if history else [], ensure_ascii=False)}"""
        self._metrics["llm_calls"] += 1
        try:
            settings = ModelSettings(max_tokens=max(1024, int(os.getenv("IT_INTENT_MAX_TOKENS", "1024"))), temperature=0.)
            run = self._classifier.run(prompt, model=self.llm, model_settings=settings)
            data: IntentClassification = (await asyncio.wait_for(run, timeout=float(os.getenv("IT_INTENT_TIMEOUT_S", "45")))).output
        except Exception as exc:
            self._metrics["fallback_calls"] += 1
            self._metrics["fallback_errors"] += 1
            self._metrics["last_error"] = type(exc).__name__
            logger.info("IT intent tool fallback: %s", type(exc).__name__)
            return {**_fallback_vote("fallback_exception"), "_error_type": type(exc).__name__}

        confidence = float(data.confidence or 0.)
        if confidence <= 0:
            confidence = .85 if data.intent in SOCIAL_INTENTS else .5
        self._metrics["llm_success"] += 1
        return {
            "intent": data.intent, "confidence": confidence, "urgency": data.urgency, "reasoning": data.reasoning[:500],
            "entities": self._clean_entities(data.entities.model_dump()), "_llm_used": True, "_source": "llm_tool_use",
        }

    async def _embedding_match(self, message: str) -> Dict[str, Any]:
        try:
            groups, query, source = await self._embedding.vectors(message, TEMPLATES, self._local_embed)
        except Exception as exc:
            logger.debug("embedding fallback: %s", type(exc).__name__)
            return {"intent": IntentCategory.OTHER, "confidence": 0., "source": "local-ngram"}
        best, best_score = IntentCategory.OTHER, 0.
        for intent, vectors in groups.items():
            score = max((sum(a * b for a, b in zip(query, vector)) for vector in vectors), default=0.)
            if score > best_score:
                best, best_score = intent, score
        return {"intent": best, "confidence": max(0., min(1., best_score)), "source": source}

    @staticmethod
    def _local_embed(text: str) -> List[float]:
        """256 维字符 trigram 哈希向量（L2 归一化），作为无外部 embedding 时的兜底。

        使用 crc32 而非内置 hash()：后者按进程随机加盐，会让同一句话在不同进程得到不同向量。
        """
        vector = [0.] * 256
        text = (text or "").lower()
        for i in range(max(0, len(text) - 2)):
            vector[zlib.crc32(text[i:i + 3].encode()) % 256] += 1.
        norm = sum(x * x for x in vector) ** .5 or 1.
        return [x / norm for x in vector]

    @staticmethod
    def _clean_entities(value: Any) -> Dict[str, List[str]]:
        if not isinstance(value, dict):
            return {}
        return {
            str(k): list(dict.fromkeys(str(x)[:64] for x in v if isinstance(x, (str, int, float))))[:8]
            for k, v in value.items() if isinstance(v, list)
        }

    @staticmethod
    def _merge_entities(a: Dict[str, List[str]], b: Dict[str, List[str]]) -> Dict[str, List[str]]:
        out = dict(a)
        for k, v in b.items():
            out[k] = list(dict.fromkeys(out.get(k, []) + v))[:8]
        return out

    @staticmethod
    def _pattern(message: str) -> Dict[str, Any]:
        lowered = message.lower()
        # 组合关键词优先于单词计数。
        if "天气" in lowered or "写一首诗" in lowered:
            return {"intent": IntentCategory.OUT_OF_SCOPE, "confidence": .9}
        for words, intent in ((("创建", "工单"), IntentCategory.TICKET_CREATE), (("取消", "工单"), IntentCategory.TICKET_CANCEL), (("检查", "权限"), IntentCategory.ACCESS_REVIEW)):
            if all(word in lowered for word in words):
                return {"intent": intent, "confidence": .8}
        if "教程" in lowered or "使用方法" in lowered:
            return {"intent": IntentCategory.HOWTO, "confidence": .8}
        intent, hits = max(((i, sum(w.lower() in lowered for w in words)) for i, words in KEYWORDS.items()), key=lambda x: x[1])
        if not hits:
            return {"intent": IntentCategory.OTHER, "confidence": 0.}
        return {"intent": intent, "confidence": min(.95, .5 + .15 * hits)}

    @staticmethod
    def _vote(llm: Dict[str, Any], embedding: Dict[str, Any], pattern: Dict[str, Any]):
        sources = {"llm": llm, "embedding": embedding, "pattern": pattern}
        scores: Dict[IntentCategory, float] = {}
        for name, weight in VOTE_WEIGHTS:
            result = sources[name]
            intent = result.get("intent", IntentCategory.OTHER)
            scores[intent] = scores.get(intent, 0.) + weight * max(0., min(1., float(result.get("confidence", 0.))))
        intent, score = max(scores.items(), key=lambda x: x[1])
        # LLM 不可用时，明确的本地规则命中优先于 OTHER 的低信息结果。
        if intent == IntentCategory.OTHER and pattern.get("intent") != IntentCategory.OTHER:
            intent, score = pattern["intent"], max(score, float(pattern.get("confidence", 0.)))
        groups = [INTENT_GROUPS.get(item.get("intent", IntentCategory.OTHER), "general") for item in sources.values()]
        group_agreement = max(groups.count(group) for group in set(groups)) / len(groups)
        source_scores = {name: float(item.get("confidence", 0.)) for name, item in sources.items()}
        return intent, max(.5, round(score, 4)), {**source_scores, "group_agreement": round(group_agreement, 4)}

    @staticmethod
    def _extract_entities(message: str) -> Dict[str, List[str]]:
        result = {k: list(dict.fromkeys(re.findall(p, message, re.I))) for k, p in ENTITY_PATTERNS.items()}
        result.update({k: re.findall(p, message, re.I) for k, p in FREE_TEXT_ENTITY_PATTERNS.items()})
        return result

    @staticmethod
    def _urgency(message: str, intent: IntentCategory) -> UrgencyLevel:
        lowered = message.lower()
        if any(x in lowered for x in ("全员", "生产中断", "critical")):
            return UrgencyLevel.CRITICAL
        if any(x in lowered for x in ("无法工作", "完全不能", "urgent", "紧急")):
            return UrgencyLevel.HIGH
        if intent in {IntentCategory.HUMAN_HANDOFF, IntentCategory.ACCOUNT_UNLOCK}:
            return UrgencyLevel.HIGH
        return UrgencyLevel.NORMAL

    @staticmethod
    def _cache_key(message: str, history: Optional[List[Dict[str, str]]], previous_intent_group: Optional[str] = None) -> str:
        return json.dumps([message, history[-4:] if history else [], previous_intent_group], ensure_ascii=False, sort_keys=True)
