import { deferred } from './fixtures.ts'
import type { ChatTransport } from '../src/lib/chatSession.ts'
import { test } from 'vitest'
import assert from 'node:assert/strict'
import { createChatSession, replyMeta } from '../src/lib/chatSession.ts'
import { normalizeChatResponse } from '../src/api/chat.ts'
import { createRequestState } from '../src/lib/requestState.ts'

const settings = () => ({ backend: 'python', endpoints: { python: 'http://api.test' }, conversationId: '', accessToken: 't' })
const reply = (raw = {}) => normalizeChatResponse('python', { request_id: 'r1', response: '已处理', intent: 'vpn_issue', intent_confidence: 0.9, ...raw })

function session(stream: ChatTransport['stream'], s = settings()) {
  let persisted = 0
  const chat = createChatSession(s, createRequestState(s), { stream, trace: async () => ({ trace: { toolsUsed: ['lookup'] } }), loadApproval: async card => { card.approval = { approval_id: card.approvalId, status: 'pending' } } },
    { persist: () => { persisted++ } })
  return { chat, settings: s, persisted: () => persisted }
}

test('a reply streams into a draft and is replaced by the final message', async () => {
  const { chat, settings: s, persisted } = session(async (_type, snapshot, _message, onEvent) => {
    onEvent('answer_delta', { delta: '已' })
    assert.equal(chat.state.messages.at(-1)!.content, '已')
    onEvent('tool_started', { tool: 'lookup' })
    assert.equal(chat.state.streamStatus, '正在调用工具 lookup')
    assert.equal(snapshot.conversationId, s.conversationId)
    return reply()
  })
  chat.state.draft = '  VPN 连不上  '
  await chat.send()
  assert.deepEqual(chat.state.messages.map(m => [m.role, m.content]), [['user', 'VPN 连不上'], ['assistant', '已处理']])
  assert.equal(chat.state.messages[1]!.meta, 'VPN 故障 · 置信度 90%')
  assert.deepEqual(chat.state.lastTrace?.trace.toolsUsed, ['lookup'])
  assert.equal(chat.state.busy, false)
  assert.equal(chat.state.draft, '')
  assert.ok(s.conversationId)
  assert.equal(persisted(), 1)
})

test('starting a new conversation discards a reply still in flight', async () => {
  const pending = deferred<ReturnType<typeof reply>>()
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
  assert.equal(chat.state.messages.at(-1)!.error, true)
  assert.equal(chat.state.messages.at(-1)!.content, '503 Service Unavailable')
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
  const card = chat.state.messages.at(-1)!.relatedApprovals![0]!
  assert.equal(card.approval?.approval_id, 'A9')
  assert.equal(card.summary, 'GitLab 只读')
})

test('opening a stored conversation restores its messages and continues under its id', async () => {
  let sent
  const { chat, settings: s, persisted } = session(async (_type, snapshot) => { sent = snapshot.conversationId; return reply() })
  chat.state.messages.push({ id: 'old', role: 'user', content: '旧对话' })
  chat.open('conv-9', [
    { request_id: 'r1', role: 'user', content: 'VPN 连不上', meta: {} },
    { request_id: 'r1', role: 'assistant', content: '请检查账号', meta: { intent: 'vpn_issue', intent_confidence: 0.9, knowledge_used: true, approval_id: 'r1:approval' } }
  ])
  assert.equal(s.conversationId, 'conv-9')
  assert.ok(persisted() >= 1)
  assert.deepEqual(chat.state.messages.map(m => [m.role, m.content]), [['user', 'VPN 连不上'], ['assistant', '请检查账号']])
  assert.equal(chat.state.messages[1]!.meta, 'VPN 故障 · 置信度 90% · RAG')
  assert.equal(chat.state.messages[1]!.approvalId, 'r1:approval')
  chat.state.draft = '还是不行'
  await chat.send()
  assert.equal(sent, 'conv-9')
})

test('stopping reception removes the streaming draft and releases the composer', async () => {
  const { chat } = session(async (_type, _settings, _content, emit, options) => {
    emit('answer_delta', { delta: '临时回复' })
    return new Promise((_resolve, reject) => options!.signal.addEventListener('abort', () => reject(options!.signal.reason)))
  })
  chat.state.draft = '检查 VPN'
  const sending = chat.send()
  assert.equal(chat.receiving, true)
  chat.stopReceiving()
  await sending
  assert.equal(chat.receiving, false)
  assert.equal(chat.state.busy, false)
  assert.deepEqual(chat.state.messages.map(message => message.role), ['user', 'assistant'])
  assert.match(chat.state.messages.at(-1)!.content, /已停止接收/)
})
