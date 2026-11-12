"""知识库导入、核对、删除与检索。"""
import asyncio
import json
import logging
import pathlib
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from api import state
from api.deps import current_principal, admin_change, require_admin
from api.schemas import KnowledgeAddRequest
from core.identity import Principal

router = APIRouter()
logger = logging.getLogger(__name__)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def _add_knowledge(documents: list[Dict[str, Any]], principal: Principal, via: str) -> Dict[str, Any]:
    if not state.kb:
        raise HTTPException(503, "service not ready")
    async with admin_change(principal, "knowledge_added", None, {
            "via": via, "documents": len(documents), "titles": [d["title"][:200] for d in documents[:20]]}) as change:
        try:
            chunks = await state.kb.add_documents_async(documents)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # 让 Agent 立即检索到新文档，而不是等待 60 秒 TTL 缓存过期。
        state.tool_manager.invalidate_cache("knowledge_search")
        change.detail["chunks"] = chunks
    return {"documents": len(documents), "chunks": chunks, "total_chunks": await state.kb.doc_count_async()}


@router.post("/knowledge/add")
async def knowledge_add(payload: KnowledgeAddRequest, principal: Principal = Depends(require_admin)):
    return await _add_knowledge([doc.model_dump(exclude_none=True) for doc in payload.documents], principal, "add")


@router.post("/knowledge/upload")
async def knowledge_upload(file: UploadFile = File(...), principal: Principal = Depends(require_admin)):
    """导入 .txt / .md（整篇为一个文档，标题取文件名）或 .json（文档数组或 {"documents": [...]})。"""
    name = pathlib.Path(file.filename or "upload.txt")
    suffix = name.suffix.lower()
    if suffix not in {".txt", ".md", ".json"}:
        raise HTTPException(415, "仅支持 .txt、.md、.json")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "文件必须是 UTF-8 编码") from exc
    if suffix != ".json":
        return await _add_knowledge([{"title": name.stem, "content": text}], principal, "upload:" + name.name[:100])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"JSON 解析失败：第 {exc.lineno} 行") from exc
    try:
        payload = KnowledgeAddRequest.model_validate(data if isinstance(data, dict) else {"documents": data})
    except Exception as exc:
        raise HTTPException(422, "JSON 需为文档数组，每项包含 title 与 content") from exc
    return await _add_knowledge([doc.model_dump(exclude_none=True) for doc in payload.documents], principal, "upload:" + name.name[:100])


@router.get("/knowledge/documents", dependencies=[Depends(require_admin)])
async def knowledge_documents(department: Optional[str] = Query(None, max_length=80)):
    """管理员按文档查看知识库（标题、来源、部门、片段数、是否为演示种子）。"""
    if not state.kb:
        raise HTTPException(503, "service not ready")
    items = await asyncio.to_thread(state.kb.list_documents, department)
    return {"items": items, "total": len(items)}


@router.delete("/knowledge/documents/{document_id}")
async def knowledge_delete(document_id: str, principal: Principal = Depends(require_admin)):
    """删除过时或错误的文档；检索缓存立即失效，Agent 下一次检索就看不到它。"""
    if not state.kb:
        raise HTTPException(503, "service not ready")
    async with admin_change(principal, "knowledge_deleted", document_id) as change:
        removed = await asyncio.to_thread(state.kb.delete_document, document_id)
        if removed is None:
            raise HTTPException(404, "document not found")
        state.tool_manager.invalidate_cache("knowledge_search")
        logger.info("knowledge document %s deleted by %s", document_id, principal.subject)
        change.detail = {k: removed[k] for k in ("title", "source", "chunks", "demo_seed")}
    return {**removed, "total_chunks": await state.kb.doc_count_async()}


@router.get("/knowledge/stats")
async def knowledge_stats():
    count = await state.kb.doc_count_async() if state.kb else 0
    # documents 是旧接口的片段数别名；前端明确展示 total_chunks。
    return {"documents": count, "total_chunks": count}


@router.post("/search")
async def search(query: str = Query("", min_length=1), top_k: int = Query(5, ge=1, le=20), department: Optional[str] = Query(None),
                 mode: str = Query("hybrid", pattern="^(hybrid|vector|bm25)$"), principal: Principal = Depends(current_principal)):
    if not state.tool_manager:
        raise HTTPException(503, "service not ready")
    # 检索范围由身份决定；只有管理员可以指定其他部门（运维排查用）。
    if not principal.is_admin or department is None:
        department = principal.department
    result = await state.tool_manager.call("knowledge_search", {"query": query, "top_k": top_k, "department": department, "mode": mode}, {})
    return result.data if result.success else {"items": [], "fallback": True, "error": result.error}
