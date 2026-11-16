import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function MarkdownMessage({ text }: { text: string }) {
  return <div className="markdown-message">
    <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
      a: ({ children, href }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
      // Model output must not automatically load remote tracking images.
      img: ({ alt }) => <span className="text-muted-foreground">[图片：{alt || '请查看附件'}]</span>,
      table: ({ children }) => <div className="overflow-x-auto"><table>{children}</table></div>
    }}>{text}</Markdown>
  </div>
}
