import type { ChatMessage } from '../src/lib/types.ts'
import { test } from 'vitest'
import assert from 'node:assert/strict'
import { createAnswerDraft } from '../src/lib/answerDraft.ts'

const ids = () => { let n = 0; return () => `m${++n}` }

test('deltas accumulate into one assistant draft', () => {
  const messages: ChatMessage[] = [{ id: 'u', role: 'user', content: 'VPN 报错' }]
  const draft = createAnswerDraft(messages, ids())
  assert.equal(draft.onEvent('answer_delta', { delta: 'VPN-691 ' }), true)
  draft.onEvent('answer_delta', { delta: '是认证失败' })
  assert.equal(messages.length, 2)
  assert.deepEqual(messages[1], { id: 'm1', role: 'assistant', content: 'VPN-691 是认证失败', meta: '生成中…' })
})

test('reset clears the preamble but keeps the same draft bubble', () => {
  const messages: ChatMessage[] = []
  const draft = createAnswerDraft(messages, ids())
  draft.onEvent('answer_delta', { delta: '我先查一下。' })
  assert.equal(draft.onEvent('answer_reset', {}), false)
  assert.equal(messages[0]!.content, '')
  draft.onEvent('answer_delta', { delta: '结论：' })
  assert.equal(messages.length, 1)
  assert.equal(messages[0]!.content, '结论：')
})

test('reset before any delta and unrelated events do nothing', () => {
  const messages: ChatMessage[] = []
  const draft = createAnswerDraft(messages, ids())
  draft.onEvent('answer_reset', {})
  const toolEvent = { tool: 'x', delta: 'ignored' }
  assert.equal(draft.onEvent('tool_started', toolEvent), false)
  assert.equal(messages.length, 0)
})

test('remove drops only the draft and is idempotent', () => {
  const messages: ChatMessage[] = [{ id: 'u', role: 'user', content: 'hi' }]
  const draft = createAnswerDraft(messages, ids())
  draft.onEvent('answer_delta', { delta: '草稿' })
  messages.push({ id: 'later', role: 'user', content: 'next' })
  draft.remove()
  draft.remove()
  assert.deepEqual(messages.map((m) => m.id), ['u', 'later'])
})
