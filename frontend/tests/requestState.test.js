import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequestState } from '../src/lib/requestState.js'

function state() { return { backend:'python', userId:'a', conversationId:'', endpoints:{python:'/py'} } }

test('changing the endpoint and back invalidates old responses', () => {
  const settings = state()
  const requests = createRequestState(settings)
  const old = requests.begin('stats')
  settings.endpoints.python = '/other'; requests.invalidate()
  settings.endpoints.python = '/py'; requests.invalidate()
  assert.equal(old.isCurrent(), false)
  assert.equal(requests.begin('stats').isCurrent(), true)
})

test('latest request wins within each slot and request settings are snapshots', () => {
  const settings = state()
  const requests = createRequestState(settings)
  const old = requests.begin('stats')
  const fresh = requests.begin('stats')
  requests.begin('monitor')
  assert.equal(old.isCurrent(), false)
  assert.equal(fresh.isCurrent(), true)
  settings.endpoints.python = '/changed'
  assert.equal(fresh.settings.endpoints.python, '/py')
  assert.equal(fresh.isCurrent(), false)
})

const source = fs.readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
function loadFunction(name, context) {
  const start = source.indexOf(`async function ${name}(`)
  const next = source.slice(start + 1).search(/\n(?:async )?function /)
  const end = next < 0 ? source.indexOf('</script>',start) : start + 1 + next
  vm.runInContext(source.slice(start,end),context)
}

for (const [name, requestName, field, result] of [
  ['loadStats','requestKnowledgeStats','knowledgeCount',{total_chunks: 99}],
  ['loadMonitor','requestMonitor','monitorData',{agents:{old:true}}],
  ['loadReports','requestEvaluationReports','evalReports',{gate:{all_passed:true},suites:[]}],
  ['loadSkills','requestSkills','skillsData',{skills:['old']}],
]) {
  for (const fail of [false,true]) {
    test(`${name}: stale ${fail ? 'error' : 'success'} cannot overwrite switched state`, async () => {
      const settings = state()
      const requests = createRequestState(settings)
      let resolve, reject
      const pending = new Promise((a,b)=>{resolve=a;reject=b})
      const context = vm.createContext({settings,requestState:requests,[field]:{value:'new'},[requestName]:()=>pending})
      loadFunction(name,context)
      const task = context[name]()
      settings.endpoints.python='/other'; requests.invalidate()
      if (fail) reject(new Error('old')); else resolve(result)
      await task
      assert.equal(context[field].value,'new')
    })
  }
}
