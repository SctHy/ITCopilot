"""IT-Copilot 企业 IT 服务台 API。"""
import asyncio, json, logging, os, pathlib, sys, uuid, time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
_ROOT=str(pathlib.Path(__file__).parent.parent.resolve()); sys.path.insert(0,_ROOT) if _ROOT not in sys.path else None
from fastapi import FastAPI, HTTPException, Request as HTTPRequest, Query
from fastapi.responses import Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
load_dotenv(pathlib.Path(_ROOT)/".env")
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO")); logger=logging.getLogger("it_copilot")
_orchestrator=None; _memory=None; _tool_manager=None; _kb=None; _skills=None
CHAT_REQUESTS=Counter("it_copilot_chat_requests_total","IT-Copilot chat requests",["status","intent_group"])
CHAT_LATENCY=Histogram("it_copilot_chat_latency_seconds","IT-Copilot chat latency",["status"],buckets=(.1,.25,.5,1,2,5,10,30,60))

class ChatRequest(BaseModel):
    message:str=Field(...,min_length=1,max_length=8000); user_id:str="demo-user"; conv_id:str="demo"; client_request_id:Optional[str]=None; department:str="engineering"
class ChatResponse(BaseModel):
    request_id:str; response:str; status:str; agent_type:str; intent:str; intent_group:str="other"; entities:Dict[str,Any]={}; confidence:float=0.; intent_confidence:float=0.; routing_confidence:float=0.; intent_source_scores:Dict[str,float]={}; clarification_needed:bool=False; missing_slots:list[str]=[]; inherited_from_previous:bool=False; trace_id:str=""; tools_used:list[str]=[]; knowledge_used:bool=False; latency_ms:float=0.; escalated:bool=False; plan:Optional[Dict[str,Any]]=None; findings:list[Dict[str,Any]]=[]; approval_required:bool=False; approval_id:Optional[str]=None; token_usage:Dict[str,int]={}

class ApprovalDecision(BaseModel):
    decision:str=Field(...,pattern="^(approved|rejected)$"); actor:str=Field("it-admin",min_length=1,max_length=120)

@asynccontextmanager
async def lifespan(app:FastAPI):
    global _orchestrator,_memory,_tool_manager,_kb,_skills
    from agents.agent_orchestrator import AgentOrchestrator
    from core.skill_loader import SkillManager
    from tooling.tool_manager import MCPToolManager, Tool
    from tooling.knowledge_base import KnowledgeBase
    from memory.conversation_memory import MemoryManager
    key=os.getenv("ANTHROPIC_API_KEY","sk-local"); base=os.getenv("ANTHROPIC_BASE_URL") or None; model=os.getenv("ANTHROPIC_MODEL","deepseek-chat")
    skill_dir=os.getenv("IT_COPILOT_SKILLS_DIR",str(pathlib.Path(_ROOT)/"skills")); _skills=SkillManager(skill_dir); _skills.load()
    _tool_manager=MCPToolManager(api_key=key,base_url=base,model=model)
    _kb=KnowledgeBase(chroma_host=os.getenv("CHROMA_HOST","127.0.0.1"),chroma_port=int(os.getenv("CHROMA_PORT","8000")),chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY",str(pathlib.Path(_ROOT)/"data/chroma")))
    async def knowledge(params,ctx=None): return await _kb.search_handler(params,ctx)
    _tool_manager.register(Tool(name="knowledge_search",description="IT 知识库混合检索",handler=knowledge,schema={"type":"object","properties":{"query":{"type":"string","minLength":1},"top_k":{"type":"integer","minimum":1,"maximum":8},"department":{"type":["string","null"]},"mode":{"type":"string","enum":["hybrid","vector","bm25"]}},"required":["query"],"additionalProperties":False},cache_ttl=60,timeout_s=8,fallback=lambda p,c,e:{"items":[],"fallback":True,"error":e}))
    _orchestrator=AgentOrchestrator(api_key=key,base_url=base,model=model,skill_manager=_skills,rag_tool_manager=_tool_manager)
    try:
        _memory=MemoryManager(redis_url=os.getenv("REDIS_URL","redis://:it-copilot123@127.0.0.1:6379/0"),chroma_host=os.getenv("CHROMA_HOST","127.0.0.1"),chroma_port=int(os.getenv("CHROMA_PORT","8000")),chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY",str(pathlib.Path(_ROOT)/"data/chroma")),api_key=key,base_url=base,model=model)
        # Redis is part of the correctness boundary: locks and idempotency are
        # not optional. A lazy client construction alone is insufficient.
        await _memory._redis.ping()
    except Exception as exc:
        _memory=None
        if os.getenv("IT_COPILOT_REQUIRE_REDIS","true").lower() not in {"0","false","no"}:
            logger.error("Redis is required for safe startup: %s", type(exc).__name__)
            raise RuntimeError("Redis 不可用，拒绝启动：会话锁和幂等保护不能被绕过") from exc
        logger.warning("memory degraded by explicit IT_COPILOT_REQUIRE_REDIS=false: %s",type(exc).__name__)
    logger.info("IT-Copilot ready on port %s",os.getenv("IT_COPILOT_PORT","8001")); yield
    if _memory:
        try: await _memory.close()
        except Exception: pass

app=FastAPI(title="IT-Copilot",version="0.1.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["http://127.0.0.1:5173","http://localhost:5173"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])

@app.get("/health")
async def health(): return {"status":"ok","service":"IT-Copilot","port":int(os.getenv("IT_COPILOT_PORT","8001")),"agents":list(_orchestrator._pool) if _orchestrator else []}
@app.get("/skills")
async def skills(): return _skills.summary() if _skills else {"count":0,"skills":[]}
@app.post("/skills/reload")
async def reload_skills(): return _skills.summary() if _skills and _skills.reload() is not None else {"count":0}
@app.get("/monitor")
async def monitor():
    tools={}
    if _tool_manager:
        for name,tool in _tool_manager._tools.items():
            tools[name]={"total":tool.stats.total,"success":tool.stats.success,"failed":tool.stats.failed,"success_rate":tool.stats.success_rate,"avg_latency_ms":tool.stats.avg_latency_ms,"circuit_state":tool.breaker.state.value}
    return {"agents":_orchestrator.get_stats() if _orchestrator else {},"tools":tools}
@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
@app.get("/trace/tool/{request_id}")
async def trace_tool(request_id: str):
    trace=await _memory.get_trace(request_id) if _memory else None
    return {"request_id":request_id,"found":trace is not None,"trace":trace or {}}
@app.get("/trace/{request_id}")
async def trace_request(request_id: str):
    """Phase 6 canonical replay endpoint for plan, tools, memory and approval events."""
    trace=await _memory.get_trace(request_id) if _memory else None
    return {"request_id":request_id,"found":trace is not None,"trace":trace or {},"schema_version":1}
@app.get("/trace/tools")
async def trace_tools(limit:int=20):
    return {"items":await _memory.recent_traces(limit) if _memory else []}
@app.get("/approvals/{approval_id}")
async def get_approval(approval_id: str):
    if not _memory: raise HTTPException(503,"approval store unavailable")
    body=await _memory.get_approval(approval_id)
    if not body: raise HTTPException(404,"approval not found")
    return body
@app.post("/approvals/{approval_id}")
async def decide_approval(approval_id: str, payload: ApprovalDecision):
    if not _memory: raise HTTPException(503,"approval store unavailable")
    try: body=await _memory.decide_approval(approval_id,payload.decision,payload.actor)
    except ValueError as exc: raise HTTPException(422,str(exc))
    if not body: raise HTTPException(404,"approval not found")
    return body
@app.get("/knowledge/stats")
async def knowledge_stats(): return {"documents":await _kb.doc_count_async() if _kb else 0}
@app.post("/search")
async def search(query: str = Query("", min_length=1), top_k: int = Query(5, ge=1, le=20), department: Optional[str] = Query(None), mode: str = Query("hybrid", pattern="^(hybrid|vector|bm25)$")):
    if not _tool_manager: raise HTTPException(503,"service not ready")
    result=await _tool_manager.call("knowledge_search",{"query":query,"top_k":top_k,"department":department,"mode":mode},{})
    return result.data if result.success else {"items":[],"fallback":True,"error":result.error}

@app.post("/chat",response_model=ChatResponse)
async def chat(payload:ChatRequest):
    if not _orchestrator: raise HTTPException(503,"service not ready")
    request_started=time.monotonic()
    rid=payload.client_request_id or str(uuid.uuid4())
    conv_id=payload.conv_id or str(uuid.uuid5(uuid.NAMESPACE_URL,payload.user_id+":"+rid))
    mem_ctx=None
    from agents.agent_orchestrator import Request
    from core.conversation_state import merge_state
    async def execute():
        nonlocal mem_ctx
        mem_ctx=await _memory.get_context(payload.user_id,conv_id,query=payload.message) if _memory else None
        history=[{"role":m.role.value,"content":m.content} for m in mem_ctx.recent_messages[-5:]] if mem_ctx and mem_ctx.recent_messages else None
        previous_group=(getattr(mem_ctx,"state",{}) or {}).get("intent_group") if mem_ctx else None
        intent_result=await _orchestrator.recognize_intent(payload.message,history=history,previous_intent_group=previous_group)
        context=mem_ctx.to_prompt_text() if mem_ctx else ""
        req=Request(message=payload.message,user_id=payload.user_id,conv_id=conv_id,context=context,history=history,entities=intent_result.entities,intent=intent_result.intent,intent_group=intent_result.intent_group,urgency=intent_result.urgency,intent_confidence=intent_result.confidence,department=payload.department,request_id=rid,conversation_state=getattr(mem_ctx,"state",{}) if mem_ctx else {})
        req.context += f"\n[部门]\n{payload.department}"
        req.conversation_state=merge_state(req.conversation_state or {},intent_result.intent_group,intent_result.entities,payload.message)
        if intent_result.missing_slots:
            req.conversation_state["missing_fields"] = sorted(set(req.conversation_state.get("missing_fields", [])) | set(intent_result.missing_slots))
        async def before_unsafe_tool(name):
            if _memory:
                await _memory.commit_turn(payload.user_id,conv_id,rid,payload.message,{"request_id":rid,"response":"tool outcome pending","status":"failed","retryable":False,"error_code":"tool_outcome_unknown"})
        req.before_unsafe_tool=before_unsafe_tool
        return await _orchestrator.run(req),req,intent_result
    if _memory:
        async with _memory.conversation(payload.user_id,conv_id):
            receipt=await _memory.get_turn_receipt(payload.user_id,conv_id,rid)
            if receipt:
                if receipt.get("message")!=payload.message: raise HTTPException(409,"同一 client_request_id 不能用于不同消息")
                return receipt.get("response")
            result,req,intent_result=await execute()
    else:
        result,req,intent_result=await execute()
    from core.approval import approval_summary
    approval=approval_summary([t.get("tool_name","") for t in result.tool_traces if t.get("success") and not t.get("retry_safe",True)], payload.message)
    approval_id=rid+":approval" if approval else None
    if approval and _memory: await _memory.create_approval(approval_id,{**approval,"request_id":rid,"user_id":payload.user_id,"conv_id":conv_id})
    response=ChatResponse(request_id=rid,response=result.response,status=result.status,agent_type=result.agent_type.value,intent=result.intent.value if result.intent else "other",intent_group=req.intent_group or "other",entities=req.entities,confidence=req.intent_confidence,intent_confidence=req.intent_confidence,routing_confidence=result.routing_confidence,intent_source_scores=intent_result.source_scores,clarification_needed=intent_result.clarification_needed,missing_slots=intent_result.missing_slots,inherited_from_previous=intent_result.inherited_from_previous,trace_id=result.trace_id,tools_used=result.tools_used,knowledge_used="search_knowledge_base" in result.tools_used,latency_ms=result.latency_ms,escalated=result.escalated,plan=result.plan,findings=result.findings,approval_required=bool(approval),approval_id=approval_id,token_usage=result.token_usage)
    if _memory:
        trace=json.loads(json.dumps(result.__dict__,default=lambda value: value.value if hasattr(value,"value") else str(value)))
        trace["observability"]={"schema_version":2,"memory":{"working_hit":bool(mem_ctx and mem_ctx.recent_messages),"episodic_hits":len(mem_ctx.relevant_history) if mem_ctx else 0,"profile_hit":bool(mem_ctx and mem_ctx.user_profile)},"intent":{"source":intent_result.recognition_source,"confidence":intent_result.confidence,"group_agreement":intent_result.source_scores.get("group_agreement")},"approval":{"required":bool(approval),"approval_id":approval_id},"fallback":{"intent_source":intent_result.recognition_source}}
        await _memory.commit_turn(payload.user_id,conv_id,rid,payload.message,response.model_dump(),trace,state=req.conversation_state)
        asyncio.create_task(_memory.update_profile(payload.user_id,conv_id))
        if result.intent and result.intent.value not in {"greeting", "chitchat"}:
            asyncio.create_task(_memory.store_episodic_turn(payload.user_id,conv_id,rid,payload.message,response.response,{"intent":result.intent.value,"intent_group":req.intent_group or "other","agent_type":result.agent_type.value,"entities":req.entities}))
    CHAT_REQUESTS.labels(status=response.status, intent_group=response.intent_group).inc()
    CHAT_LATENCY.labels(status=response.status).observe(time.monotonic()-request_started)
    return response

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

@app.post("/chat/stream")
async def chat_stream(payload: ChatRequest):
    """SSE facade preserving the normal chat transaction and idempotency path."""
    async def generate():
        yield _sse("status", {"stage":"started","request_id":payload.client_request_id})
        try:
            raw=await chat(payload)
            response=raw if isinstance(raw,ChatResponse) else ChatResponse.model_validate(raw)
            if response.plan: yield _sse("plan", response.plan)
            for finding in response.findings: yield _sse("step", finding)
            for tool in response.tools_used: yield _sse("tool", {"name":tool,"status":"finished"})
            if response.approval_required: yield _sse("approval_required", {"approval_id":response.approval_id})
            text=response.response
            for index in range(0,len(text),80):
                yield _sse("content_delta", {"delta":text[index:index+80]})
                await asyncio.sleep(0)
            yield _sse("completed", response.model_dump())
        except HTTPException as exc:
            yield _sse("error", {"status_code":exc.status_code,"detail":exc.detail})
        except Exception:
            logger.exception("SSE chat failed")
            yield _sse("error", {"status_code":500,"detail":"stream_failed"})
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

