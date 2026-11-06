// 回答草稿：answer_delta 边生成边追加到一条助手消息；answer_reset 表示刚才那段是调用工具前的过渡语，清空重来；
// 完成或失败时移除草稿，由最终回答替代。messages 是响应式数组，草稿要从数组里取回代理对象再修改。
export function createAnswerDraft(messages, createId) {
  let draft = null
  return {
    onEvent(event, data) {
      if (event === 'answer_delta') {
        if (!draft) {
          messages.push({ id: createId(), role: 'assistant', content: '', meta: '生成中…' })
          draft = messages[messages.length - 1]
        }
        draft.content += data.delta
        return true
      }
      if (event === 'answer_reset' && draft) draft.content = ''
      return false
    },
    remove() {
      if (draft) {
        const index = messages.indexOf(draft)
        if (index >= 0) messages.splice(index, 1)
      }
      draft = null
    }
  }
}
