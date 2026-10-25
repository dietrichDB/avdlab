import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { learnApi, type SchemaChild, type SchemaEntry, type SchemaName } from '../../api/phase6'
import { ErrorBox, Modal, Spinner } from '../ui'
import { cls } from '../../lib/format'

const childKey = (parent: string, name: string) => (parent ? `${parent}.${name}` : name)
const childrenOf = (e: SchemaEntry | undefined): SchemaChild[] => (e ? (e.keys?.length ? e.keys : e.items?.keys ?? []) : [])
const fmtVal = (v: unknown) => (v === null || v === undefined ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v))

function useDebounced<T>(v: T, ms = 250): T {
  const [d, setD] = useState(v)
  useEffect(() => { const t = setTimeout(() => setD(v), ms); return () => clearTimeout(t) }, [v, ms])
  return d
}

/** Schema browser: fuzzy search + lazily expanded key tree + detail of the selected key. */
export function SchemaBrowser({ initialKey = '', initialSchema = 'eos_designs', className }: { initialKey?: string; initialSchema?: SchemaName; className?: string }) {
  const [schema, setSchema] = useState<SchemaName>(initialSchema)
  const [selected, setSelected] = useState(initialKey)
  const [text, setText] = useState('')
  const dq = useDebounced(text)
  useEffect(() => { setSelected(initialKey) }, [initialKey])
  useEffect(() => { setSchema(initialSchema) }, [initialSchema])
  const hits = useQuery({ queryKey: ['learn', 'schema-search', dq], queryFn: () => learnApi.schemaSearch(dq), enabled: dq.trim().length >= 2 })
  const detail = useQuery({ queryKey: ['learn', 'schema', schema, selected], queryFn: () => learnApi.schema(schema, selected), enabled: !!selected, retry: false })

  return (
    <div className={cls('grid gap-3 lg:grid-cols-[22rem_1fr]', className)} data-testid="schema-browser">
      <div className="card flex min-h-0 flex-col gap-2 p-2">
        <div className="flex gap-1">
          <input className="input py-1 text-xs" placeholder="search keys (e.g. loopback pool)" value={text} onChange={(e) => setText(e.target.value)} data-testid="schema-search" />
          <select className="input w-auto py-1 text-xs" value={schema} onChange={(e) => { setSchema(e.target.value as SchemaName); setSelected('') }} aria-label="schema">
            <option value="eos_designs">eos_designs</option>
            <option value="eos_cli_config_gen">eos_cli_config_gen</option>
          </select>
        </div>
        {dq.trim().length >= 2 ? (
          <ul className="max-h-[60vh] overflow-auto text-xs" data-testid="schema-hits">
            {hits.isLoading && <Spinner />}
            <ErrorBox error={hits.error} />
            {(hits.data ?? []).map((h) => (
              <li key={`${h.schema}:${h.key}`}>
                <button className="w-full rounded px-1.5 py-1 text-left hover:bg-zinc-100 dark:hover:bg-zinc-800" onClick={() => { setSchema(h.schema); setSelected(h.key); setText('') }}>
                  <span className="mono block truncate font-medium">{h.key}</span>
                  <span className="block truncate text-[11px] text-zinc-500">{h.schema} · {h.type} · {h.short}</span>
                </button>
              </li>
            ))}
            {hits.data && !hits.data.length && <li className="p-2 text-zinc-500">No matches.</li>}
          </ul>
        ) : (
          <ul className="max-h-[60vh] overflow-auto text-xs" data-testid="schema-tree">
            <TreeLevel schema={schema} parent="" depth={0} selected={selected} onSelect={setSelected} />
          </ul>
        )}
      </div>
      <div className="card min-w-0 p-3">
        {!selected ? <div className="text-sm text-zinc-500">Pick a key in the tree or search. Paths are dotted, list items without an index: <span className="mono">l3leaf.node_groups.nodes.id</span>.</div>
          : detail.isLoading ? <Spinner /> : detail.error ? <ErrorBox error={detail.error} /> : detail.data ? <SchemaDetail entry={detail.data} onSelect={setSelected} /> : null}
      </div>
    </div>
  )
}

function TreeLevel({ schema, parent, depth, selected, onSelect }: { schema: SchemaName; parent: string; depth: number; selected: string; onSelect: (k: string) => void }) {
  const q = useQuery({ queryKey: ['learn', 'schema', schema, parent], queryFn: () => learnApi.schema(schema, parent), staleTime: Infinity })
  if (q.isLoading) return <li className="pl-2 text-zinc-400" style={{ paddingLeft: depth * 12 + 4 }}>…</li>
  if (q.error) return <li><ErrorBox error={q.error} /></li>
  return <>{childrenOf(q.data).map((c) => <TreeItem key={c.name} schema={schema} parent={parent} child={c} depth={depth} selected={selected} onSelect={onSelect} />)}</>
}

function TreeItem({ schema, parent, child, depth, selected, onSelect }: { schema: SchemaName; parent: string; child: SchemaChild; depth: number; selected: string; onSelect: (k: string) => void }) {
  const key = childKey(parent, child.name)
  const [open, setOpen] = useState(selected.startsWith(key + '.'))
  const container = /dict|list/.test(child.type ?? '')
  return (
    <li>
      <div className={cls('flex items-center gap-1 rounded py-0.5 pr-1', selected === key ? 'bg-blue-100 dark:bg-blue-900/50' : 'hover:bg-zinc-100 dark:hover:bg-zinc-800')} style={{ paddingLeft: depth * 12 + 4 }}>
        <button className="w-3 text-zinc-400" onClick={() => setOpen((o) => !o)} disabled={!container} aria-label={open ? `collapse ${key}` : `expand ${key}`}>{container ? (open ? '▾' : '▸') : ''}</button>
        <button className="min-w-0 flex-1 truncate text-left" onClick={() => onSelect(key)} title={child.short} data-testid="schema-node">
          <span className={cls('mono', child.deprecated && 'line-through opacity-60')}>{child.name}</span>
          <span className="ml-1 text-[10px] text-zinc-400">{child.type}</span>
        </button>
      </div>
      {open && container && <ul><TreeLevel schema={schema} parent={key} depth={depth + 1} selected={selected} onSelect={onSelect} /></ul>}
    </li>
  )
}

export function SchemaDetail({ entry: e, onSelect }: { entry: SchemaEntry; onSelect?: (k: string) => void }) {
  const kids = childrenOf(e)
  return (
    <div className="space-y-2 text-sm" data-testid="schema-detail">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="mono text-base font-semibold break-all">{e.key || '(top level)'}</span>
        <span className="rounded bg-zinc-100 px-1 text-xs dark:bg-zinc-800">{e.type ?? '?'}{e.items?.type ? `[${e.items.type}]` : ''}</span>
        {e.required && <span className="rounded bg-red-100 px-1 text-xs text-red-800 dark:bg-red-900/50 dark:text-red-200">required</span>}
        {!!e.deprecation && <span className="rounded bg-amber-100 px-1 text-xs text-amber-800 dark:bg-amber-900/50 dark:text-amber-200">deprecated</span>}
        <span className="text-xs text-zinc-500">{e.schema}</span>
      </div>
      {e.alias_of && <div className="text-xs text-zinc-500">schema path <span className="mono">{e.alias_of}</span></div>}
      {e.description && <p className="whitespace-pre-line text-zinc-700 dark:text-zinc-300">{e.description}</p>}
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-xs">
        <dt className="text-zinc-500">default</dt><dd className="mono break-all">{fmtVal(e.default)}</dd>
        {e.valid_values?.length ? <><dt className="text-zinc-500">valid values</dt><dd className="mono break-all">{e.valid_values.map(String).join(' | ')}</dd></> : null}
        {e.min != null || e.max != null ? <><dt className="text-zinc-500">range</dt><dd className="mono">{e.min ?? '−∞'} … {e.max ?? '∞'}</dd></> : null}
        {e.format ? <><dt className="text-zinc-500">format</dt><dd className="mono">{e.format}</dd></> : null}
        {e.primary_key || e.items?.primary_key ? <><dt className="text-zinc-500">primary key</dt><dd className="mono">{e.primary_key ?? e.items?.primary_key}</dd></> : null}
        {e.deprecation ? <><dt className="text-zinc-500">deprecation</dt><dd>{fmtVal(e.deprecation)}</dd></> : null}
      </dl>
      <div className="flex gap-3 text-xs">
        {e.doc_url && <a className="link" href={e.doc_url} target="_blank" rel="noreferrer" data-testid="schema-doc">AVD docs table →</a>}
        {e.section_url && <a className="link" href={e.section_url} target="_blank" rel="noreferrer">data model section</a>}
      </div>
      {kids.length > 0 && (
        <div>
          <div className="h2 mb-1">Keys ({kids.length})</div>
          <table className="table text-xs">
            <tbody>{kids.map((c) => (
              <tr key={c.name}>
                <td className="mono">{onSelect ? <button className="link" onClick={() => onSelect(childKey(e.key, c.name))}>{c.name}</button> : c.name}{c.required && <span className="text-red-600">*</span>}</td>
                <td className="text-zinc-500">{c.type}</td>
                <td>{c.short}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/** Opened from the YAML editor (Ctrl/Cmd-click a key or the "?" button). */
export function SchemaModal({ open, onClose, schemaKey, schema }: { open: boolean; onClose: () => void; schemaKey: string; schema: SchemaName }) {
  return (
    <Modal open={open} onClose={onClose} title={<span>Schema — <span className="mono">{schemaKey}</span></span>} width="max-w-5xl">
      <SchemaBrowser initialKey={schemaKey} initialSchema={schema} />
    </Modal>
  )
}
