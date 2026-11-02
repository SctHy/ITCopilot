"""知识库导入与检索。"""
import json
import pathlib
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from api import state
from api.deps import current_principal, require_admin
from api.schemas import KnowledgeAddRequest
from core.identity import Principal

router = APIRouter()


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def _add_knowledge(documents: list[Dict[str, Any]]) -> Dict[str, Any]:
    if not state.kb:
        raise HTTPException(503, "service not ready")
    try:
        chunks = await state.kb.add_documents_async(documents)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # 让 Agent 立即检索到新文档，而不是等待 60 秒 TTL 缓存过期。
    state.tool_manager.invalidate_cache("knowledge_search")
    return {"documents": len(documents), "chunks": chunks, "total_chunks": await state.kb.doc_count_async()}


@router.post("/knowledge/add", dependencies=[Depends(require_admin)])
async def knowledge_add(payload: KnowledgeAddRequest):
    return await _add_knowledge([doc.model_dump(exclude_none=True) for doc in payload.documents])


@router.post("/knowledge/upload", dependencies=[Depends(require_admin)])
async def knowledge_upload(file: UploadFile = File(...)):
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
        return await _add_knowledge([{"title": name.stem, "content": text}])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"JSON 解析失败：第 {exc.lineno} 行") from exc
    try:
        payload = KnowledgeAddRequest.model_validate(data if isinstance(data, dict) else {"documents": data})
    except Exception as exc:
        raise HTTPException(422, "JSON 需为文档数组，每项包含 title 与 content") from exc
    return await _add_knowledge([doc.model_dump(exclude_none=True) for doc in payload.documents])


@router.get("/knowledge/stats")
async def knowledge_stats():
    return {"documents": await state.kb.doc_count_async() if state.kb else 0}


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
