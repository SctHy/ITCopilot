import { test } from 'node:test'
import assert from 'node:assert/strict'
import { normalizeToolTraceResponse } from '../src/lib/backends.js'

function detailedTrace() {
  return {
    request_id: 'req-1',
    found: true,
    trace: {
      request_id: 'req-1',
      tools_used: ['search_knowledge_base', 'lookup_error_code', 'check_service_status'],
      tool_traces: [
        { agent_type: 'network_software', tool_name: 'search_knowledge_base', tool_use_id: 'call-1', input: { query: 'VPN 401' }, success: true, latency_ms: 12, error: '', retry_safe: true },
        { agent_type: 'network_software', tool_name: 'lookup_error_code', tool_use_id: 'call-2', input: { error_code: '401' }, success: true, latency_ms: 0.5, error: '', retry_safe: true },
        { agent_type: 'network_software', tool_name: 'check_service_status', tool_use_id: 'call-3', input: { service: 'corporate-vpn' }, success: false, latency_ms: 3, error: 'status adapter unavailable', retry_safe: true }
      ]
    }
  }
}

test('maps backend tool_traces into toolCalls with detailed fields', () => {
  const out = normalizeToolTraceResponse(detailedTrace())
  assert.equal(out.trace.toolCalls.length, 3)
  assert.equal(out.trace.toolsUsed.length, 3)
  assert.equal(out.trace.toolCalls[0].tool_name, 'search_knowledge_base')
  assert.deepEqual(out.trace.toolCalls[0].input, { query: 'VPN 401' })
  assert.equal(out.trace.toolCalls[0].success, true)
  assert.equal(out.trace.toolCalls[2].success, false)
  assert.equal(out.trace.toolCalls[2].error, 'status adapter unavailable')
})

test('falls back to legacy tool_calls shape for older responses', () => {
  const legacy = {
    request_id: 'req-2',
    found: true,
    trace: {
      tools_used: ['reset_password'],
      tool_calls: [{ tool_name: 'reset_password', input: { employee_id: '1001' }, success: true }]
    }
  }
  const out = normalizeToolTraceResponse(legacy)
  assert.equal(out.trace.toolCalls.length, 1)
  assert.equal(out.trace.toolCalls[0].tool_name, 'reset_password')
})

test('keeps toolsUsed when detailed tool_traces are absent', () => {
  const out = normalizeToolTraceResponse({ request_id: 'req-3', found: true, trace: { tools_used: ['inspect_request_context'] } })
  assert.deepEqual(out.trace.toolCalls, [])
  assert.deepEqual(out.trace.toolsUsed, ['inspect_request_context'])
})

test('returns empty lists for an empty or missing trace without throwing', () => {
  const empty = normalizeToolTraceResponse({})
  assert.equal(empty.found, false)
  assert.deepEqual(empty.trace.toolCalls, [])
  assert.deepEqual(empty.trace.toolsUsed, [])
})
