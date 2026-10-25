import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import {
  ReactFlow, Background, Controls, MiniMap, Handle, Position, ConnectionMode, useNodesState,
  type NodeProps, type Node, type Edge, type Connection,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { Topology } from '../api/types'
import { toFlow, roleColour, type FlowNode, type FlowEdge } from '../lib/topology'

type DeviceData = FlowNode['data'] & { editable?: boolean; selected?: boolean }
type DeviceNode = Node<DeviceData, 'device'>

function DeviceNodeView({ data }: NodeProps<DeviceNode>) {
  const c = roleColour[data.role]
  const h = { opacity: data.editable ? 0.6 : 0, width: 8, height: 8, background: c.border }
  return (
    <div className={`rounded border-2 px-2 py-1 text-[11px] leading-tight text-zinc-900 shadow-sm ${data.selected ? 'ring-2 ring-blue-500 ring-offset-1' : ''}`}
      style={{ background: c.bg, borderColor: c.border, minWidth: 110 }} data-testid={`topo-node-${data.label}`}>
      <Handle type="target" position={Position.Top} style={h} />
      <div className="font-semibold">{data.label}</div>
      <div className="text-[10px] text-zinc-600">{data.role} · {data.kind}{data.mgmt_ip ? ` · ${data.mgmt_ip}` : ''}</div>
      <Handle type="source" position={Position.Bottom} style={h} />
    </div>
  )
}

const nodeTypes = { device: DeviceNodeView }

export type Positions = Record<string, { x: number; y: number }>

export interface TopologyGraphProps {
  topology: Topology
  className?: string
  /** stored/draft positions; missing nodes fall back to the layered auto-layout */
  positions?: Positions | null
  /** bump to re-seed positions (auto-layout reset) */
  resetKey?: number
  editable?: boolean
  /** node drag allowed (edit mode and layout not locked) */
  draggable?: boolean
  /** link indexes whose ends may be dragged to another node (uplinks) */
  reconnectableLinks?: Set<number>
  selected?: string | null
  onPositionsChange?: (p: Positions) => void
  onEdgeRetarget?: (link: number, source: string, target: string, at: { x: number; y: number }) => void
  onNodeClick?: (name: string) => void
  onNodeContextMenu?: (name: string, at: { x: number; y: number }) => void
  children?: ReactNode
}

export function TopologyGraph({
  topology, className = 'h-[520px]', positions, resetKey = 0, editable = false, draggable = false, reconnectableLinks, selected,
  onPositionsChange, onEdgeRetarget, onNodeClick, onNodeContextMenu, children,
}: TopologyGraphProps) {
  const flow = useMemo(() => toFlow(topology, { positions }), [topology, positions])
  const [nodes, setNodes, onNodesChange] = useNodesState<DeviceNode>([])
  const box = useRef<HTMLDivElement>(null)
  const lastPointer = useRef({ x: 0, y: 0 })
  const nodesRef = useRef(nodes)
  useEffect(() => { nodesRef.current = nodes }, [nodes])

  // re-seed on new topology / positions / reset (resetKey is an explicit trigger)
  useEffect(() => {
    setNodes(flow.nodes.map((n) => ({ ...n, data: { ...n.data } })))
  }, [flow, resetKey, setNodes])

  const shown = useMemo(
    () => nodes.map((n) => ({ ...n, draggable, data: { ...n.data, editable, selected: n.id === selected } })),
    [nodes, draggable, editable, selected],
  )
  const edges: Edge[] = useMemo(() => flow.edges.map((e: FlowEdge) => {
    const re = editable && !!reconnectableLinks?.has(e.data.link)
    return {
      ...e, type: 'straight', reconnectable: re, labelStyle: { fontSize: 9 }, labelBgStyle: { fillOpacity: 0.7 },
      style: { stroke: re ? '#0ea5e9' : '#94a3b8', strokeWidth: re ? 2 : 1 },
    }
  }), [flow, editable, reconnectableLinks])

  const rel = (x: number, y: number) => {
    const r = box.current?.getBoundingClientRect()
    return r ? { x: x - r.left, y: y - r.top } : { x, y }
  }

  return (
    <div ref={box} className={`card relative overflow-hidden ${className}`} data-testid="topology-graph"
      onPointerUpCapture={(e) => { lastPointer.current = rel(e.clientX, e.clientY) }}>
      <ReactFlow
        nodes={shown}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        fitView
        nodesConnectable={false}
        edgesReconnectable={editable}
        connectionMode={ConnectionMode.Loose}
        onReconnect={(old: Edge, c: Connection) => {
          const link = (old.data as FlowEdge['data'] | undefined)?.link
          if (link !== undefined && c.source && c.target) onEdgeRetarget?.(link, c.source, c.target, lastPointer.current)
        }}
        onNodeDragStop={(_e, _n, dragged) => {
          const moved = new Map(dragged.map((d) => [d.id, d.position]))
          onPositionsChange?.(Object.fromEntries(nodesRef.current.map((n) => {
            const p = moved.get(n.id) ?? n.position
            return [n.id, { x: Math.round(p.x), y: Math.round(p.y) }]
          })))
        }}
        onNodeClick={(_, n) => onNodeClick?.(n.id)}
        onNodeContextMenu={(e, n) => {
          if (!onNodeContextMenu) return
          e.preventDefault()
          onNodeContextMenu(n.id, rel(e.clientX, e.clientY))
        }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.2}
      >
        <Background gap={16} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeColor={(n) => roleColour[(n.data as FlowNode['data']).role]?.border ?? '#999'} />
      </ReactFlow>
      {children}
    </div>
  )
}

export function TopologyLegend() {
  return (
    <div className="flex flex-wrap gap-3 text-xs">
      {(Object.keys(roleColour) as (keyof typeof roleColour)[]).map((r) => (
        <span key={r} className="inline-flex items-center gap-1">
          <span className="inline-block h-3 w-3 rounded border-2" style={{ background: roleColour[r].bg, borderColor: roleColour[r].border }} />
          {r}
        </span>
      ))}
    </div>
  )
}
