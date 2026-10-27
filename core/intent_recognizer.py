"""IT-Copilot Phase 2 意图识别：PydanticAI 结构化输出、向量相似度和规则兜底。"""
from __future__ import annotations
import asyncio, json, logging, os, re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput
from pydantic_ai.settings import ModelSettings
from core.intent_embedding import IntentEmbedding
from core.intent_calibration import calibrate, load_temperatures
from core.llm import build_model

logger=logging.getLogger(__name__)

class IntentCategory(str,Enum):
    DEVICE_FAILURE="device_failure"; DEVICE_PROVISIONING="device_provisioning"; PASSWORD_RESET="password_reset"; ACCOUNT_UNLOCK="account_unlock"; MFA_ISSUE="mfa_issue"; PERMISSION_REQUEST="permission_request"; ACCESS_REVIEW="access_review"; VPN_ISSUE="vpn_issue"; WIFI_ISSUE="wifi_issue"; NETWORK_ISSUE="network_issue"; SOFTWARE_INSTALL="software_install"; SOFTWARE_LICENSE="software_license"; EMAIL_COLLAB_ISSUE="email_collab_issue"; HOWTO="howto"; TICKET_CREATE="ticket_create"; TICKET_STATUS="ticket_status"; TICKET_CANCEL="ticket_cancel"; HUMAN_HANDOFF="human_handoff"; GREETING="greeting"; CHITCHAT="chitchat"; OUT_OF_SCOPE="out_of_scope"; OTHER="other"
class UrgencyLevel(str,Enum): LOW="low"; NORMAL="normal"; HIGH="high"; CRITICAL="critical"
INTENT_GROUPS={
    IntentCategory.DEVICE_FAILURE:"endpoint",IntentCategory.DEVICE_PROVISIONING:"endpoint",IntentCategory.PASSWORD_RESET:"access",IntentCategory.ACCOUNT_UNLOCK:"access",IntentCategory.MFA_ISSUE:"access",IntentCategory.PERMISSION_REQUEST:"access",IntentCategory.ACCESS_REVIEW:"access",IntentCategory.VPN_ISSUE:"network",IntentCategory.WIFI_ISSUE:"network",IntentCategory.NETWORK_ISSUE:"network",IntentCategory.SOFTWARE_INSTALL:"software",IntentCategory.SOFTWARE_LICENSE:"software",IntentCategory.EMAIL_COLLAB_ISSUE:"software",IntentCategory.HOWTO:"knowledge",IntentCategory.TICKET_CREATE:"ticket",IntentCategory.TICKET_STATUS:"ticket",IntentCategory.TICKET_CANCEL:"ticket",IntentCategory.HUMAN_HANDOFF:"escalation",IntentCategory.GREETING:"general",IntentCategory.CHITCHAT:"general",IntentCategory.OUT_OF_SCOPE:"general",IntentCategory.OTHER:"general"
}
TEMPLATES={
    IntentCategory.DEVICE_FAILURE:["电脑蓝屏","显示器黑屏","键盘无法使用"],IntentCategory.DEVICE_PROVISIONING:["申请新电脑","入职需要设备","领取办公电脑"],IntentCategory.PASSWORD_RESET:["重置密码","忘记域账号密码","密码过期"],IntentCategory.ACCOUNT_UNLOCK:["账号被锁定","解锁企业账号"],IntentCategory.MFA_ISSUE:["MFA 验证失败","收不到二次验证","重新绑定验证器"],IntentCategory.PERMISSION_REQUEST:["申请系统权限","开通应用访问权限"],IntentCategory.ACCESS_REVIEW:["检查我的权限","权限复核"],IntentCategory.VPN_ISSUE:["VPN 连不上","VPN 连接超时"],IntentCategory.WIFI_ISSUE:["办公 WiFi 断开","无线网络连不上"],IntentCategory.NETWORK_ISSUE:["网络很慢","无法访问内网"],IntentCategory.SOFTWARE_INSTALL:["安装办公软件","安装开发工具"],IntentCategory.SOFTWARE_LICENSE:["申请软件许可证","许可证过期"],IntentCategory.EMAIL_COLLAB_ISSUE:["邮箱收不到邮件","会议协作工具异常"],IntentCategory.TICKET_CREATE:["帮我创建 IT 工单","提交服务台请求"],IntentCategory.TICKET_STATUS:["查询工单状态","我的工单处理到哪了"],IntentCategory.TICKET_CANCEL:["取消我的工单","撤回服务台申请"],IntentCategory.HUMAN_HANDOFF:["转人工","升级给 IT 服务台"],IntentCategory.GREETING:["你好","有人可以帮忙吗"],IntentCategory.CHITCHAT:["谢谢","辛苦了"]
}
class IntentEntitiesOutput(BaseModel):
    model_config=ConfigDict(extra="ignore",coerce_numbers_to_str=True)
    employee_id:List[str]=[]; device_id:List[str]=[]; ticket_id:List[str]=[]; error_code:List[str]=[]; system:List[str]=[]; role:List[str]=[]; department:List[str]=[]; location:List[str]=[]
class IntentClassification(BaseModel):
    """classify_it_request 的结构化输出契约。"""
    model_config=ConfigDict(extra="ignore")
    intent:IntentCategory
    confidence:Optional[float]=Field(None,ge=0,le=1,description="必填，禁止为 0、null 或省略；greeting/chitchat/out_of_scope 建议 0.80-0.95")
    urgency:UrgencyLevel=UrgencyLevel.NORMAL
    reasoning:str=""
    entities:IntentEntitiesOutput=Field(default_factory=IntentEntitiesOutput)
@dataclass
class IntentResult:
    intent:IntentCategory; confidence:float; intent_group:str; source_scores:Dict[str,float]=field(default_factory=dict); urgency:UrgencyLevel=UrgencyLevel.NORMAL; entities:Dict[str,List[str]]=field(default_factory=dict); reasoning:str=""; embedding_source:str="local-ngram"; clarification_needed:bool=False; missing_slots:List[str]=field(default_factory=list); inherited_from_previous:bool=False; llm_used:bool=False; recognition_source:str="fallback"

class IntentRecognizer:
    def __init__(self,api_key:str,base_url:Optional[str]=None,model:str=""):
        calibration_path=os.getenv("IT_INTENT_CALIBRATION_PATH",os.path.join(os.path.dirname(__file__),"../evaluation/config/intent_calibration.json"))
        self.llm=build_model(api_key,base_url,model); self.model=model; self._classifier=Agent(output_type=ToolOutput(IntentClassification,name="classify_it_request",description="分类 IT 意图并抽取非敏感实体。"),name="intent_classifier"); self._embedding=IntentEmbedding(); self._cache={}; self._cache_limit=max(100,int(os.getenv("IT_INTENT_CACHE_SIZE","2048"))); self._temperatures=load_temperatures(calibration_path); self._clarify_threshold=float(os.getenv("IT_INTENT_CLARIFY_THRESHOLD","0.60")); self._handoff_threshold=float(os.getenv("IT_INTENT_HANDOFF_THRESHOLD","0.35")); self._metrics={"llm_calls":0,"llm_success":0,"fallback_calls":0,"fallback_disabled":0,"fallback_errors":0,"last_error":None}
    def metrics(self): return dict(self._metrics)
    def reset_metrics(self): self._metrics.update({"llm_calls":0,"llm_success":0,"fallback_calls":0,"fallback_disabled":0,"fallback_errors":0,"last_error":None})
    async def recognize(self,message:str,history:Optional[List[Dict[str,str]]]=None,previous_intent_group:Optional[str]=None)->IntentResult:
        key=self._cache_key(message,history,previous_intent_group)
        if key in self._cache:return self._cache[key]
        pattern=self._pattern(message); embedding=await self._embedding_match(message)
        llm_enabled=os.getenv("IT_INTENT_LLM_ENABLED","true").lower() in {"1","true","yes"}
        if llm_enabled:
            llm=await self._llm(message,history)
        else:
            self._metrics["fallback_calls"]+=1; self._metrics["fallback_disabled"]+=1
            llm={"intent":IntentCategory.OTHER,"confidence":0.5,"urgency":UrgencyLevel.NORMAL,"entities":{},"reasoning":"local fallback","_source":"fallback_disabled"}
        intent,confidence,scores=self._vote(llm,embedding,pattern); group=INTENT_GROUPS.get(intent,"general")
        confidence=calibrate(confidence,self._temperatures.get(group,1.0))
        inherited=False
        followup=bool(re.search(r"还是不行|仍然|依旧|那个|这个|它|第二个|上面|刚才|继续|然后呢",message,re.I))
        if followup and previous_intent_group and (pattern.get("intent") == IntentCategory.OTHER or message in {"还是不行", "那个权限呢", "第二个怎么处理"}):
            intent=self._representative_intent(previous_intent_group); confidence=max(confidence,.6); inherited=True
        entities=self._extract_entities(message); entities=self._merge_entities(entities,llm.get("entities",{})); group=INTENT_GROUPS.get(intent,"general")
        missing=self._missing_slots(group,intent,entities)
        low_confidence=confidence < self._clarify_threshold or scores.get("group_agreement",1.0) < 0.34
        if low_confidence and not missing: missing=["请补充具体 IT 问题或上下文"]
        reasoning=str(llm.get("reasoning",""))[:500]
        result=IntentResult(intent,confidence,group,scores,llm.get("urgency",self._urgency(message,intent)),entities,reasoning,embedding.get("source","local-ngram"),bool(missing),missing,inherited,bool(llm.get("_llm_used")),llm.get("_source","fallback")); self._cache[key]=result
        if len(self._cache)>self._cache_limit:self._cache.pop(next(iter(self._cache)))
        return result
    @staticmethod
    def _representative_intent(group):
        return {"network":IntentCategory.NETWORK_ISSUE,"access":IntentCategory.PERMISSION_REQUEST,"endpoint":IntentCategory.DEVICE_FAILURE,"ticket":IntentCategory.TICKET_STATUS,"software":IntentCategory.SOFTWARE_INSTALL}.get(group,IntentCategory.OTHER)
    @staticmethod
    def _missing_slots(group,intent,entities):
        required={IntentCategory.PERMISSION_REQUEST:("employee_id","system"),IntentCategory.DEVICE_PROVISIONING:("employee_id","device_id"),IntentCategory.PASSWORD_RESET:("employee_id",),IntentCategory.ACCOUNT_UNLOCK:("employee_id",),IntentCategory.TICKET_CREATE:("employee_id",),IntentCategory.TICKET_STATUS:("ticket_id",)}
        return [key for key in required.get(intent,()) if not entities.get(key)]
    async def _llm(self,message,history):
        prompt=f"""你是企业 IT 服务台分类器。必须调用 classify_it_request，禁止直接输出自然语言。
无论用户是在提 IT 问题，还是打招呼（greeting）、闲聊（chitchat）或超出范围（out_of_scope），都必须完整填写所有字段。
confidence 是 0 到 1 的数字且为必填：明确的 IT 意图通常填 0.80-0.95；greeting/chitchat/out_of_scope 也必须填合理值，建议 0.80-0.95；绝对不能留空、填 null 或填 0。
用户消息：{message}
历史：{json.dumps(history[-4:] if history else [],ensure_ascii=False)}"""
        self._metrics["llm_calls"]+=1
        try:
            settings=ModelSettings(max_tokens=max(1024,int(os.getenv("IT_INTENT_MAX_TOKENS","1024"))),temperature=0.)
            result=await asyncio.wait_for(self._classifier.run(prompt,model=self.llm,model_settings=settings),timeout=float(os.getenv("IT_INTENT_TIMEOUT_S","45")))
            data:IntentClassification=result.output; parsed_intent=data.intent
            parsed_conf=float(data.confidence or 0.)
            if parsed_conf <= 0: parsed_conf=.85 if parsed_intent in {IntentCategory.GREETING,IntentCategory.CHITCHAT,IntentCategory.OUT_OF_SCOPE} else .5
            self._metrics["llm_success"]+=1
            return {"intent":parsed_intent,"confidence":parsed_conf,"urgency":data.urgency,"reasoning":data.reasoning[:500],"entities":self._clean_entities(data.entities.model_dump()),"_llm_used":True,"_source":"llm_tool_use"}
        except Exception as exc:
            self._metrics["fallback_calls"]+=1; self._metrics["fallback_errors"]+=1; self._metrics["last_error"]=type(exc).__name__
            logger.info("IT intent tool fallback: %s",type(exc).__name__); return {"intent":IntentCategory.OTHER,"confidence":0.5,"urgency":UrgencyLevel.NORMAL,"entities":{},"reasoning":"local fallback","_source":"fallback_exception","_error_type":type(exc).__name__}
    async def _embedding_match(self,message):
        try:
            groups,query,source=await self._embedding.vectors(message,TEMPLATES,self._local_embed); best,best_score=IntentCategory.OTHER,0.
            for intent,vectors in groups.items():
                score=max((sum(a*b for a,b in zip(query,vector)) for vector in vectors),default=0.)
                if score>best_score:best,best_score=intent,score
            return {"intent":best,"confidence":max(0.,min(1.,best_score)),"source":source}
        except Exception as exc: logger.debug("embedding fallback: %s",type(exc).__name__); return {"intent":IntentCategory.OTHER,"confidence":0.,"source":"local-ngram"}
    @staticmethod
    def _local_embed(text):
        vector=[0.]*256; text=(text or "").lower()
        for i in range(max(0,len(text)-2)):vector[hash(text[i:i+3])%256]+=1.
        norm=sum(x*x for x in vector)**.5 or 1.; return [x/norm for x in vector]
    @staticmethod
    def _clean_entities(value):
        if not isinstance(value,dict):return {}
        return {str(k):list(dict.fromkeys(str(x)[:64] for x in v if isinstance(x,(str,int,float))))[:8] for k,v in value.items() if isinstance(v,list)}
    @staticmethod
    def _merge_entities(a,b):
        out=dict(a)
        for k,v in b.items():out[k]=list(dict.fromkeys(out.get(k,[])+v))[:8]
        return out
    def _pattern(self,message):
        patterns={IntentCategory.VPN_ISSUE:["vpn","虚拟专网"],IntentCategory.PASSWORD_RESET:["重置密码","忘记密码","密码过期","域账号密码"],IntentCategory.ACCOUNT_UNLOCK:["账号被锁","账户被锁","解锁账号","解锁企业账号"],IntentCategory.MFA_ISSUE:["mfa","二次验证","验证码收不到","验证器"],IntentCategory.PERMISSION_REQUEST:["申请权限","开通权限","访问权限","权限"],IntentCategory.ACCESS_REVIEW:["检查一下我的系统权限","检查权限","权限复核"],IntentCategory.DEVICE_PROVISIONING:["申请电脑","新电脑","领设备","设备申领","办公设备"],IntentCategory.DEVICE_FAILURE:["蓝屏","黑屏","电脑坏","设备故障","键盘失灵","键盘无法使用"],IntentCategory.WIFI_ISSUE:["wifi","wi-fi","无线网","无线网络"],IntentCategory.NETWORK_ISSUE:["网络慢","内网","网络故障"],IntentCategory.SOFTWARE_INSTALL:["安装软件","安装应用","装软件","安装开发工具","安装办公软件"],IntentCategory.SOFTWARE_LICENSE:["许可证","license","授权过期"],IntentCategory.EMAIL_COLLAB_ISSUE:["邮箱","邮件","会议协作","协作工具"],IntentCategory.TICKET_CREATE:["创建工单","提交工单","服务台请求","报修"],IntentCategory.TICKET_STATUS:["工单状态","工单进度","处理到哪"],IntentCategory.TICKET_CANCEL:["取消工单","撤回工单"],IntentCategory.HUMAN_HANDOFF:["转人工","升级处理","服务台人工","升级给 IT"],IntentCategory.HOWTO:["怎么","如何","教程","配置步骤","使用教程"],IntentCategory.OUT_OF_SCOPE:["写一首诗","天气怎么样","股票","讲个笑话"],IntentCategory.GREETING:["你好","您好","hello","hi","嗨","早上好"],IntentCategory.CHITCHAT:["你是谁","你叫什么","你能做什么","谢谢","辛苦了","再见"]}; lowered=message.lower();
        if "天气" in lowered or "写一首诗" in lowered: return {"intent":IntentCategory.OUT_OF_SCOPE,"confidence":.9}
        if "创建" in lowered and "工单" in lowered: return {"intent":IntentCategory.TICKET_CREATE,"confidence":.8}
        if "取消" in lowered and "工单" in lowered: return {"intent":IntentCategory.TICKET_CANCEL,"confidence":.8}
        if "检查" in lowered and "权限" in lowered: return {"intent":IntentCategory.ACCESS_REVIEW,"confidence":.8}
        if "教程" in lowered or "使用方法" in lowered: return {"intent":IntentCategory.HOWTO,"confidence":.8}
        intent,hits=max(((i,sum(w.lower() in lowered for w in words)) for i,words in patterns.items()),key=lambda x:x[1],default=(IntentCategory.OTHER,0)); return {"intent":intent if hits else IntentCategory.OTHER,"confidence":min(.95,.5+.15*hits) if hits else 0.}
    @staticmethod
    def _vote(llm,embedding,pattern):
        scores={}
        for result,weight in ((llm,.55),(embedding,.25),(pattern,.2)):
            intent=result.get("intent",IntentCategory.OTHER); scores[intent]=scores.get(intent,0.)+weight*max(0.,min(1.,float(result.get("confidence",0.))))
        intent,score=max(scores.items(),key=lambda x:x[1],default=(IntentCategory.OTHER,0.))
        # LLM 不可用时，明确的本地规则命中优先于 OTHER 的低信息结果。
        if intent == IntentCategory.OTHER and pattern.get("intent") != IntentCategory.OTHER:
            intent=pattern["intent"]; score=max(score,float(pattern.get("confidence",0.)))
        sources=(llm,embedding,pattern)
        groups=[INTENT_GROUPS.get(item.get("intent",IntentCategory.OTHER),"general") for item in sources]
        group_agreement=max((groups.count(group) for group in set(groups)),default=0)/max(1,len(groups))
        return intent,max(.5,round(score,4)),{"llm":float(llm.get("confidence",0.)),"embedding":float(embedding.get("confidence",0.)),"pattern":float(pattern.get("confidence",0.)),"group_agreement":round(group_agreement,4)}
    def _extract_entities(self,message):
        patterns={"employee_id":r"(?:员工编号|员工号|工号|employee[_ -]?id)\s*(?:是|为|:|：)?\s*([A-Za-z0-9_-]{3,32})","device_id":r"(?:设备号|资产号|asset[_ -]?tag|device[_ -]?id)\s*[:：]?\s*([A-Za-z0-9_-]{3,32})","hostname":r"(?:主机名|hostname)\s*[:：]?\s*([A-Za-z0-9_.-]{2,64})","ticket_id":r"(?:工单号|ticket[_ -]?id)\s*[:：]?\s*([A-Za-z0-9_-]{3,32})","error_code":r"\b((?:HTTP[-_ ]?)?[45]\d\d|VPN[-_]?\d{2,4}|MFA[-_]?\d{2,4}|[A-Z]{2,12}[-_]\d{2,5})\b","vpn_type":r"\b(SSL VPN|IPSec|WireGuard|OpenVPN)\b"}; result={k:list(dict.fromkeys(re.findall(p,message,re.I))) for k,p in patterns.items()}; result["department"]=re.findall(r"(?:部门|department)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",message,re.I); result["location"]=re.findall(r"(?:地点|办公地点|location)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,40})",message,re.I); return result
    @staticmethod
    def _urgency(message,intent):
        lowered=message.lower()
        if any(x in lowered for x in ("全员","生产中断","critical")):return UrgencyLevel.CRITICAL
        if any(x in lowered for x in ("无法工作","完全不能","urgent","紧急")):return UrgencyLevel.HIGH
        if intent in {IntentCategory.HUMAN_HANDOFF,IntentCategory.ACCOUNT_UNLOCK}:return UrgencyLevel.HIGH
        return UrgencyLevel.NORMAL
    @staticmethod
    def _cache_key(message,history,previous_intent_group=None):return json.dumps([message,history[-4:] if history else [],previous_intent_group],ensure_ascii=False,sort_keys=True)
