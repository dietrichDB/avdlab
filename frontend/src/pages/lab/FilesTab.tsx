import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { labsApi } from '../../api/labs'
import { ApiError } from '../../api/client'
import type { Lab, Violation } from '../../api/types'
import { FileTree } from '../../components/FileTree'
import { YamlEditor } from '../../components/YamlEditor'
import { ErrorBox, Spinner } from '../../components/ui'
import { parseYaml } from '../../lib/yaml'
import { fmtBytes } from '../../lib/format'
import { useSearchParams } from 'react-router-dom'
import { ConfigViewer } from '../../components/learn/ConfigExplain'
import { SchemaModal } from '../../components/learn/SchemaBrowser'
import { useLearningMode } from '../../hooks/useLearn'
import { configHost, schemaForFile, yamlPathAt } from '../../lib/learn'

export default function FilesTab({ lab }: { lab: Lab }) {
  const qc = useQueryClient()
  const files = useQuery({ queryKey: ['labs', lab.id, 'files'], queryFn: () => labsApi.files(lab.id) })
  const [params, setParams] = useSearchParams()
  const [path, setPathState] = useState<string | null>(() => params.get('path'))
  const [initialLine, setInitialLine] = useState<string | null>(() => params.get('line'))
  const [learning] = useLearningMode()
  const [schemaKey, setSchemaKey] = useState<string | null>(null)
  const setPath = (p: string) => { setPathState(p); setInitialLine(null); if (params.get('path')) setParams({}, { replace: true }) }
  // lessons navigate here with ?path=&line=
  useEffect(() => {
    const p = params.get('path')
    if (p) { setPathState(p); setInitialLine(params.get('line')) }
  }, [params])
  const entry = files.data?.find((f) => f.path === path)
  const content = useQuery({
    queryKey: ['labs', lab.id, 'file', path],
    queryFn: async () => {
      const r = await labsApi.file(lab.id, path!)
      return typeof r === 'string' ? r : r.content
    },
    enabled: !!path,
    staleTime: Infinity,
  })
  const [text, setText] = useState('')
  const [violations, setViolations] = useState<Violation[]>([])
  const [savedAt, setSavedAt] = useState<string | null>(null)
  useEffect(() => { if (content.data !== undefined) setText(content.data) }, [content.data])
  useEffect(() => { setViolations([]); setSavedAt(null) }, [path])

  // default to the first group_vars file
  useEffect(() => {
    if (!path && files.data?.length) setPathState(files.data.find((f) => f.path.startsWith('group_vars/'))?.path ?? files.data[0].path)
  }, [files.data, path])

  const isYaml = !!path && /\.ya?ml$/.test(path)
  const host = configHost(path)
  const schema = schemaForFile(path)
  const editable = entry?.editable ?? (!!path && (path.startsWith('group_vars/') || path.startsWith('host_vars/') || path === 'inventory.yml'))
  const dirty = content.data !== undefined && text !== content.data
  const parse = isYaml ? parseYaml(text) : { ok: true as const, value: null }

  const save = useMutation({
    mutationFn: () => labsApi.saveFile(lab.id, path!, text),
    onSuccess: () => {
      setViolations([])
      setSavedAt(new Date().toLocaleTimeString())
      qc.setQueryData(['labs', lab.id, 'file', path], text)
      qc.invalidateQueries({ queryKey: ['labs', lab.id] })
    },
    onError: (e) => { if (e instanceof ApiError && e.status === 422) setViolations(e.violations) },
  })

  return (
    <div className="grid h-[calc(100vh-11rem)] min-h-[400px] grid-cols-[16rem_1fr] gap-3">
      <div className="card overflow-auto p-1">
        {files.isLoading ? <Spinner /> : <ErrorBox error={files.error} />}
        {files.data && <FileTree entries={files.data} selected={path} onSelect={setPath} dirty={dirty && path ? new Set([path]) : undefined} />}
      </div>
      <div className="flex min-h-0 flex-col gap-2">
        {!path ? <div className="text-sm text-zinc-500">Select a file.</div> : <>
          <div className="flex items-center gap-2 text-xs">
            <span className="mono font-semibold">{path}</span>
            {entry?.size != null && <span className="text-zinc-500">{fmtBytes(entry.size)}</span>}
            {!editable && <span className="rounded bg-zinc-200 px-1 dark:bg-zinc-800">read-only (generated)</span>}
            {entry?.knob_managed && <span className="rounded bg-amber-100 px-1 text-amber-800 dark:bg-amber-900/50 dark:text-amber-200" title="Editing by hand marks the related knobs as custom">knob-managed</span>}
            <span className="flex-1" />
            {savedAt && <span className="text-emerald-600">saved {savedAt}</span>}
            {!parse.ok && <span className="text-red-600">YAML: {parse.error}</span>}
            {editable && <>
              <button className="btn-secondary btn-sm" disabled={!dirty} onClick={() => setText(content.data ?? '')}>Revert</button>
              <button className="btn-primary btn-sm" disabled={!dirty || !parse.ok || save.isPending} onClick={() => save.mutate()}>{save.isPending ? 'Validating…' : 'Save'}</button>
            </>}
          </div>
          {violations.length > 0 && (
            <ul className="max-h-40 list-disc overflow-auto rounded border border-red-300 bg-red-50 px-6 py-1 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200" data-testid="file-violations">
              {violations.map((v, i) => <li key={i}>{v.path && <span className="mono mr-1">{v.path}:</span>}{v.message}</li>)}
            </ul>
          )}
          {save.error && !(save.error instanceof ApiError && save.error.status === 422) && <ErrorBox error={save.error} />}
          <div className="min-h-0 flex-1">
            {content.isLoading ? <Spinner /> : content.error ? <ErrorBox error={content.error} /> : learning && host ? (
              <ConfigViewer labId={lab.id} node={host} text={text} initialLine={initialLine} height="calc(100vh - 17rem)" />
            ) : (
              <YamlEditor value={text} onChange={editable ? setText : undefined} readOnly={!editable} height="calc(100vh - 15rem)"
                onKeyHelp={learning && schema && isYaml ? (no) => setSchemaKey(yamlPathAt(text, no) ?? '') : undefined} />
            )}
          </div>
        </>}
      </div>
      {schema && schemaKey !== null && <SchemaModal open onClose={() => setSchemaKey(null)} schemaKey={schemaKey} schema={schema} />}
    </div>
  )
}
