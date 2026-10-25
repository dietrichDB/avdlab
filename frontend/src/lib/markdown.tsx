import { Fragment, type ReactNode } from 'react'

/**
 * Minimal, safe markdown → React renderer (no HTML injection): headings, paragraphs, lists, tables, fenced code,
 * blockquotes, rules; inline code, bold, italics, links. Enough for CHANGELOG.md / THIRD_PARTY_NOTICES.md / LICENSE.
 */
export function Markdown({ source, className }: { source: string; className?: string }) {
  return <div className={className ?? 'md space-y-2 text-sm'}>{renderBlocks(source)}</div>
}

const safeHref = (u: string) => (/^(https?:\/\/|mailto:|\/|#)/i.test(u) ? u : undefined)

export function renderInline(text: string, keyBase = 'i'): ReactNode[] {
  const out: ReactNode[] = []
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*|__[^_]+__)|(\[[^\]]+\]\([^)\s]+\))|(<https?:\/\/[^>\s]+>)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g
  let last = 0
  let m: RegExpExecArray | null
  let k = 0
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const t = m[0]
    const key = `${keyBase}-${k++}`
    if (m[1]) out.push(<code key={key} className="rounded bg-zinc-100 px-1 font-mono text-[0.85em] dark:bg-zinc-800">{t.slice(1, -1)}</code>)
    else if (m[2]) out.push(<strong key={key}>{renderInline(t.slice(2, -2), key)}</strong>)
    else if (m[3]) {
      const lm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(t)!
      const href = safeHref(lm[2])
      out.push(href ? <a key={key} className="link" href={href} target={href.startsWith('http') ? '_blank' : undefined} rel="noreferrer">{renderInline(lm[1], key)}</a> : <Fragment key={key}>{lm[1]}</Fragment>)
    } else if (m[4]) out.push(<a key={key} className="link" href={t.slice(1, -1)} target="_blank" rel="noreferrer">{t.slice(1, -1)}</a>)
    else if (m[5]) out.push(<em key={key}>{renderInline(t.slice(1, -1), key)}</em>)
    last = m.index + t.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

const cells = (row: string) => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())

export function renderBlocks(src: string): ReactNode[] {
  const lines = src.replace(/\r\n/g, '\n').split('\n')
  const out: ReactNode[] = []
  let i = 0
  let k = 0
  while (i < lines.length) {
    const line = lines[i]
    const key = `b${k++}`
    if (!line.trim() || /^<!--.*-->\s*$/.test(line.trim())) { i++; continue }
    const fence = /^```/.exec(line)
    if (fence) {
      const body: string[] = []
      i++
      while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++])
      i++
      out.push(<pre key={key} className="overflow-x-auto rounded bg-zinc-100 p-2 font-mono text-xs dark:bg-zinc-800">{body.join('\n')}</pre>)
      continue
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line)
    if (h) {
      const level = h[1].length
      const cls = ['text-lg font-semibold', 'text-base font-semibold mt-3', 'text-sm font-semibold mt-2', 'text-sm font-medium', 'text-sm', 'text-sm'][level - 1]
      const Tag = `h${Math.min(level + 1, 6)}` as 'h2'
      out.push(<Tag key={key} className={cls}>{renderInline(h[2], key)}</Tag>)
      i++
      continue
    }
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push(<hr key={key} className="border-zinc-200 dark:border-zinc-800" />); i++; continue }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const head = cells(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]))
      out.push(
        <div key={key} className="overflow-x-auto">
          <table className="table">
            <thead><tr>{head.map((c, j) => <th key={j}>{renderInline(c, `${key}h${j}`)}</th>)}</tr></thead>
            <tbody>{rows.map((r, ri) => <tr key={ri}>{r.map((c, j) => <td key={j}>{renderInline(c, `${key}r${ri}c${j}`)}</td>)}</tr>)}</tbody>
          </table>
        </div>,
      )
      continue
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line)
      const items: string[] = []
      while (i < lines.length && (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        if (/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) items.push(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ''))
        else items[items.length - 1] += ' ' + lines[i].trim()
        i++
      }
      const L = ordered ? 'ol' : 'ul'
      out.push(<L key={key} className={`${ordered ? 'list-decimal' : 'list-disc'} space-y-0.5 pl-5`}>{items.map((t, j) => <li key={j}>{renderInline(t, `${key}-${j}`)}</li>)}</L>)
      continue
    }
    if (/^>\s?/.test(line)) {
      const body: string[] = []
      while (i < lines.length && /^>\s?/.test(lines[i])) body.push(lines[i++].replace(/^>\s?/, ''))
      out.push(<blockquote key={key} className="border-l-4 border-zinc-300 pl-3 text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">{renderBlocks(body.join('\n'))}</blockquote>)
      continue
    }
    const para: string[] = []
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|>|\s*([-*+]|\d+[.)])\s+)/.test(lines[i]) && !/^\s*\|.*\|\s*$/.test(lines[i])) para.push(lines[i++].trim())
    if (!para.length) { para.push(lines[i].trim()); i++ }
    out.push(<p key={key}>{renderInline(para.join(' '), key)}</p>)
  }
  return out
}
