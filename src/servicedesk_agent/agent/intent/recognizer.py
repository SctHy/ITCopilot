"""Structured intent classification with a local rules fallback."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import replace, dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.usage import RunUsage
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput

from servicedesk_agent.agent.intent.explicit import explicit_access_systems, explicit_write_requests, readonly_requested
from servicedesk_agent.agent.intent.entities import clean_entities, extract_entities, merge_entities
from servicedesk_agent.agent.intent.taxonomy import (
    CATEGORY_GUIDE,
    INTENT_GROUPS,
    KEYWORDS,
    REPRESENTATIVE_INTENTS,
    REQUIRED_SLOTS,
    WRITE_ACTIONS,
    IntentCategory,
    UrgencyLevel,
)
from servicedesk_agent.infra.env import env_float
from servicedesk_agent.agent.model_router import ModelRouter
from servicedesk_agent.infra.monitor import metrics

logger = logging.getLogger(__name__)


I = IntentCategory
FOLLOWUP_CUES = re.compile(r"还是不行|仍然|依旧|那个|这个|它|第二个|上面|刚才|继续|然后呢", re.I)
# 这些组不代表具体 IT 领域；追问句命中它们时应继承上一轮的意图组。
NON_DOMAIN_GROUPS = {"general", "knowledge"}
SOCIAL_INTENTS = {I.GREETING, I.CHITCHAT, I.OUT_OF_SCOPE}
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
    """classify_it_request 的结构化返回格式。"""
    model_config = ConfigDict(extra="ignore")
    intent: IntentCategory
    confidence: Optional[float] = Field(None, ge=0, le=1, description="必填，禁止为 0、null 或省略；greeting/chitchat/out_of_scope 建议 0.80-0.95")
    urgency: UrgencyLevel = UrgencyLevel.NORMAL
    reasoning: str = ""
    entities: IntentEntitiesOutput = Field(default_factory=IntentEntitiesOutput)
    requested_actions: Optional[List[str]] = Field(None, description=(
        "用户这一轮明确要求办理的写操作，取值 " + "、".join(sorted(WRITE_ACTIONS)) + "；只是咨询、没要求办理时填 []"))


@dataclass
class IntentResult:
    intent: IntentCategory
    confidence: float
    intent_group: str
    source_scores: Dict[str, float] = field(default_factory=dict)
    urgency: UrgencyLevel = UrgencyLevel.NORMAL
    entities: Dict[str, List[str]] = field(default_factory=dict)
    reasoning: str = ""
    clarification_needed: bool = False
    missing_slots: List[str] = field(default_factory=list)
    inherited_from_previous: bool = False
    llm_used: bool = False
    recognition_source: str = "fallback"
    requested_actions: Optional[FrozenSet[str]] = None
    needs_knowledge: Optional[bool] = None
    token_usage: Dict[str, int] = field(default_factory=dict)


def _fresh_metrics() -> Dict[str, Any]:
    return {"llm_calls": 0, "llm_success": 0, "fallback_calls": 0, "fallback_disabled": 0, "fallback_errors": 0, "last_error": None}


def _fallback_vote(source: str) -> Dict[str, Any]:
    # 不携带 urgency：LLM 不可用时紧急度由本地规则 _urgency 判断。
    return {"intent": IntentCategory.OTHER, "confidence": 0.5, "entities": {}, "reasoning": "local fallback", "_source": source}


class IntentRecognizer:
    def __init__(self, api_key: str, base_url: Optional[str] = None, model: str = "", router=None):
        self.router = ModelRouter(api_key=api_key, base_url=base_url, model=model) if router is None else router
        self.model = model
        self._classifier = Agent(output_type=ToolOutput(IntentClassification, name="classify_it_request", description="分类 IT 意图并抽取非敏感实体。"), name="intent_classifier")
        self._cache: Dict[str, IntentResult] = {}
        self._cache_limit = max(100, int(os.getenv("IT_INTENT_CACHE_SIZE", "2048")))
        self._clarify_threshold = env_float("IT_INTENT_CLARIFY_THRESHOLD", 0.60)
        self._metrics = _fresh_metrics()

    @property
    def llm(self):
        return self.router.models[self.router.config.routes["intent_fallback"].candidates[0]]

    @llm.setter
    def llm(self, model):
        self.router.replace_models(model)

    def metrics(self) -> Dict[str, Any]:
        return dict(self._metrics)

    def reset_metrics(self) -> None:
        self._metrics = _fresh_metrics()

    async def recognize(self, message: str, history: Optional[List[Dict[str, str]]] = None, previous_intent_group: Optional[str] = None) -> IntentResult:
        key = self._cache_key(message, history, previous_intent_group)
        metrics.CACHE_LOOKUPS.labels(cache="intent", result="hit" if key in self._cache else "miss").inc()
        if key in self._cache:
            return replace(self._cache[key], token_usage={})

        pattern = self._pattern(message)
        if os.getenv("IT_INTENT_LLM_ENABLED", "true").lower() in {"1", "true", "yes"}:
            llm = await self._llm(message, history)
        else:
            self._metrics["fallback_calls"] += 1
            self._metrics["fallback_disabled"] += 1
            llm = _fallback_vote("fallback_disabled")

        usage = dict(llm.get('_usage', {}))
        intent, confidence, scores = self.select(llm, pattern)
        # LLM 已结合历史给出领域意图时不再用规则覆盖（否则“还是不行”会把 vpn_issue 改成组代表 network_issue）。
        llm_domain = llm.get("_llm_used") and INTENT_GROUPS.get(intent, "general") not in NON_DOMAIN_GROUPS
        inherited_intent = None if llm_domain else self._inherited_intent(message, pattern["intent"], previous_intent_group)
        if inherited_intent is not None:
            intent, confidence = inherited_intent, max(confidence, .6)
        group = INTENT_GROUPS.get(intent, "general")

        entities = merge_entities(extract_entities(message), llm.get("entities", {}))
        if not entities.get("system") and (systems := explicit_access_systems(message)):
            entities["system"] = systems
        missing = self._missing_slots(intent, entities)
        low_confidence = confidence < self._clarify_threshold
        explicit = explicit_write_requests(message)
        # An explicitly stated request is not a vague message: asking "请补充具体问题" would block it.
        # Fields the request actually lacks are still in ``missing``.
        if low_confidence and not missing and not explicit:
            missing = ["请补充具体 IT 问题或上下文"]

        result = IntentResult(
            intent=intent,
            token_usage=usage,
            confidence=confidence,
            intent_group=group,
            source_scores=scores,
            urgency=llm.get("urgency") or self._urgency(message, intent),
            entities=entities,
            reasoning=str(llm.get("reasoning", ""))[:500],
            clarification_needed=bool(missing),
            missing_slots=missing,
            inherited_from_previous=inherited_intent is not None,
            llm_used=bool(llm.get("_llm_used")),
            recognition_source=llm.get("_source", "fallback"),
        )
        # 业务类别不是办理授权；以独立的动作判断和明确请求为准。
        if llm.get("_actions") is not None:
            result.requested_actions = frozenset(llm["_actions"] | explicit)
        if readonly_requested(message):
            result.requested_actions = explicit
        metrics.INTENT_RECOGNITIONS.labels(source=result.recognition_source, intent_group=group).inc()
        if result.clarification_needed:
            metrics.INTENT_CLARIFICATIONS.labels(intent_group=group).inc()
        if result.inherited_from_previous:
            metrics.INTENT_INHERITED.labels(intent_group=group).inc()
        self._cache[key] = result
        if len(self._cache) > self._cache_limit:
            self._cache.pop(next(iter(self._cache)))
        return result

    @staticmethod
    def select(llm: Dict[str, Any], pattern: Dict[str, Any]):
        """LLM 结果优先；LLM 失败或被关闭时用规则结果，并在分数里标明 pattern_fallback。"""
        if str(llm.get("_source", "")).startswith("fallback"):
            return pattern["intent"], float(pattern.get("confidence", 0)), {"pattern_fallback": float(pattern.get("confidence", 0))}
        return llm["intent"], float(llm.get("confidence", 0)), {"llm": float(llm.get("confidence", 0))}

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
    def _missing_slots(intent: IntentCategory, entities: Dict[str, List[str]]) -> List[str]:
        return [key for key in REQUIRED_SLOTS.get(intent, ()) if not entities.get(key)]

    async def _llm(self, message: str, history: Optional[List[Dict[str, str]]]) -> Dict[str, Any]:
        prompt = f"""你是企业 IT 服务台分类器。必须调用 classify_it_request，禁止直接输出自然语言。
无论用户是在提 IT 问题，还是打招呼（greeting）、闲聊（chitchat）或超出范围（out_of_scope），都必须完整填写所有字段。
confidence 是 0 到 1 的数字且为必填：明确的 IT 意图通常填 0.80-0.95；greeting/chitchat/out_of_scope 也必须填合理值，建议 0.80-0.95；绝对不能留空、填 null 或填 0。
{CATEGORY_GUIDE}
结合历史解析“最开始那个”等指代；历史里的助手建议不等于用户办理意愿。查询进度、总结已有申请不属于新的写操作。
用户消息：{message}
历史：{json.dumps(history[-20:] if history else [], ensure_ascii=False)}"""
        self._metrics["llm_calls"] += 1
        measured = RunUsage()
        try:
            routed = self.router.pick("intent_fallback")
            settings = {**routed.settings, "max_tokens": max(1024, int(os.getenv("IT_INTENT_MAX_TOKENS", "1024"))), "temperature": 0.}
            run = self._classifier.run(prompt, model=routed.model, model_settings=settings, usage=measured)
            data: IntentClassification = (await self.router.run(routed, asyncio.wait_for(
                run, timeout=self.router.config.routes["intent_fallback"].timeout_s))).output
        except Exception as exc:
            self._metrics["fallback_calls"] += 1
            self._metrics["fallback_errors"] += 1
            self._metrics["last_error"] = type(exc).__name__
            logger.info("IT intent tool fallback: %s", type(exc).__name__)
            return {**_fallback_vote("fallback_exception"), "_error_type": type(exc).__name__,
                    "_usage": {"requests": measured.requests, "input_tokens": measured.input_tokens, "output_tokens": measured.output_tokens}}

        confidence = float(data.confidence or 0.)
        if confidence <= 0:
            confidence = .85 if data.intent in SOCIAL_INTENTS else .5
        self._metrics["llm_success"] += 1
        return {
            "intent": data.intent, "confidence": confidence, "urgency": data.urgency, "reasoning": data.reasoning[:500],
            "entities": clean_entities(data.entities.model_dump()), "_llm_used": True, "_source": "llm_tool_use",
            "_usage": {"requests": measured.requests, "input_tokens": measured.input_tokens, "output_tokens": measured.output_tokens,
                       "cache_read_tokens": measured.cache_read_tokens},
            "_actions": None if data.requested_actions is None else set(data.requested_actions) & set(WRITE_ACTIONS),
        }

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
        return json.dumps([message, history[-20:] if history else [], previous_intent_group], ensure_ascii=False, sort_keys=True)
