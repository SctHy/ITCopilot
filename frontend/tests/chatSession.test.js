import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createChatSession, replyMeta } from '../src/lib/chatSession.ts'
import { normalizeChatResponse } from '../src/api/chat.ts'
import { createRequestState } from '../src/lib/requestState.ts'

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b }); return { promise, resolve, reject } }
const settings = () => ({ backend: 'python', endpoints: { python: 'http://api.test' }, conversationId: '', accessToken: 't' })
const reply = (raw = {}) => normalizeChatResponse('python', { request_id: 'r1', response: '已处理', intent: 'vpn_issue', intent_confidence: 0.9, ...raw })

function session(stream, s = settings()) {
  let persisted = 0
  const chat = createChatSession(s, createRequestState(s), { stream, trace: async () => ({ trace: { toolsUsed: ['lookup'] } }), loadApproval: async card => { card.approval = { approval_id: card.approvalId, status: 'pending' } } },
    { persist: () => { persisted++ } })
  return { chat, settings: s, persisted: () => persisted }
}

test('a reply streams into a draft and is replaced by the final message', async () => {
  const { chat, settings: s, persisted } = session(async (type, snapshot, message, onEvent) => {
    onEvent('answer_delta', { delta: '已' })
    assert.equal(chat.state.messages.at(-1).content, '已')
    onEvent('tool_started', { tool: 'lookup' })
    assert.equal(chat.state.streamStatus, '正在调用工具 lookup')
    assert.equal(snapshot.conversationId, s.conversationId)
    return reply()
  })
  chat.state.draft = '  VPN 连不上  '
  await chat.send()
  assert.deepEqual(chat.state.messages.map(m => [m.role, m.content]), [['user', 'VPN 连不上'], ['assistant', '已处理']])
  assert.equal(chat.state.messages[1].meta, 'VPN 故障 · 置信度 90%')
  assert.deepEqual(chat.state.lastTrace.trace.toolsUsed, ['lookup'])
  assert.equal(chat.state.busy, false)
  assert.equal(chat.state.draft, '')
  assert.ok(s.conversationId)
  assert.equal(persisted(), 1)
})

test('starting a new conversation discards a reply still in flight', async () => {
  const pending = deferred()
  const { chat } = session(() => pending.promise)
  chat.state.draft = '旧问题'
  const sending = chat.send()
  assert.equal(chat.state.busy, true)
  chat.clear()
  pending.resolve(reply())
  await sending
  assert.deepEqual(chat.state.messages, [])
  assert.equal(chat.state.busy, false)
})

test('a failed request becomes an error message and frees the composer', async () => {
  const { chat } = session(async () => { throw new Error('503 Service Unavailable') })
  chat.state.draft = '你好'
  await chat.send()
  assert.equal(chat.state.messages.at(-1).error, true)
  assert.equal(chat.state.messages.at(-1).content, '503 Service Unavailable')
  assert.equal(chat.state.busy, false)
})

test('reply meta lists clarification, approval, RAG and escalation', () => {
  assert.equal(replyMeta(reply({ clarification_needed: true, missing_slots: ['系统'], approval_required: true, approval_id: 'A1', knowledge_used: true, escalated: true })),
    'VPN 故障 · 置信度 90% · 待补充：系统 · 待审批 · A1 · RAG · 转人工')
})

test('related approvals in a reply are loaded through the transport', async () => {
  const { chat } = session(async () => reply({ related_approvals: [{ approval_id: 'A9', kind: 'withdraw', summary: 'GitLab 只读' }] }))
  chat.state.draft = '撤回申请'
  await chat.send()
  const card = chat.state.messages.at(-1).relatedApprovals[0]
  assert.equal(card.approval.approval_id, 'A9')
  assert.equal(card.summary, 'GitLab 只读')
})
