import { useMemo, useState } from 'react'
import type { FileEntry } from '../api/types'
import { cls } from '../lib/format'

interface TreeNode { name: string; path: string; dir: boolean; children: TreeNode[]; size?: number }

/** Accepts either a nested tree (children[]) or a flat list of paths from GET /labs/{id}/files. */
export function buildTree(entries: FileEntry[]): TreeNode[] {
  const root: TreeNode = { name: '', path: '', dir: true, children: [] }
  const ensureDir = (parts: string[]): TreeNode => {
    let cur = root
    let p = ''
    for (const part of parts) {
      p = p ? `${p}/${part}` : part
      let next = cur.children.find((c) => c.dir && c.name === part)
      if (!next) {
        next = { name: part, path: p, dir: true, children: [] }
        cur.children.push(next)
      }
      cur = next
    }
    return cur
  }
  const walk = (list: FileEntry[], prefix: string) => {
    for (const e of list) {
      const full = e.path.startsWith(prefix) || !prefix ? e.path : `${prefix}/${e.path}`
      const parts = full.split('/').filter(Boolean)
      const isDir = e.type === 'dir' || !!e.children
      if (isDir) {
        const d = ensureDir(parts)
        if (e.children) walk(e.children, full)
        void d
      } else {
        const dir = ensureDir(parts.slice(0, -1))
        dir.children.push({ name: parts[parts.length - 1], path: parts.join('/'), dir: false, children: [], size: e.size })
      }
    }
  }
  walk(entries, '')
  const sort = (n: TreeNode) => {
    n.children.sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name) : a.dir ? -1 : 1))
    n.children.forEach(sort)
  }
  sort(root)
  return root.children
}

export function FileTree({ entries, selected, onSelect, dirty = new Set() }: {
  entries: FileEntry[]; selected: string | null; onSelect: (path: string) => void; dirty?: Set<string>
}) {
  const tree = useMemo(() => buildTree(entries), [entries])
  return (
    <ul className="text-xs" data-testid="file-tree">
      {tree.map((n) => <Node key={n.path} node={n} depth={0} selected={selected} onSelect={onSelect} dirty={dirty} />)}
    </ul>
  )
}

function Node({ node, depth, selected, onSelect, dirty }: {
  node: TreeNode; depth: number; selected: string | null; onSelect: (p: string) => void; dirty: Set<string>
}) {
  const [open, setOpen] = useState(depth < 2 || /group_vars|inventory/.test(node.path))
  const pad = { paddingLeft: `${depth * 12 + 4}px` }
  if (node.dir) {
    return (
      <li>
        <button className="flex w-full items-center gap-1 py-0.5 text-left text-zinc-600 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800" style={pad} onClick={() => setOpen((o) => !o)}>
          <span className="w-3 text-zinc-400">{open ? '▾' : '▸'}</span>
          <span>{node.name}/</span>
        </button>
        {open && <ul>{node.children.map((c) => <Node key={c.path} node={c} depth={depth + 1} selected={selected} onSelect={onSelect} dirty={dirty} />)}</ul>}
      </li>
    )
  }
  return (
    <li>
      <button
        className={cls('flex w-full items-center gap-1 py-0.5 text-left hover:bg-zinc-100 dark:hover:bg-zinc-800', selected === node.path && 'bg-blue-100 font-medium dark:bg-blue-900/50')}
        style={pad}
        onClick={() => onSelect(node.path)}
      >
        <span className="w-3" />
        <span className="mono truncate">{node.name}</span>
        {dirty.has(node.path) && <span className="text-amber-500">●</span>}
      </button>
    </li>
  )
}
