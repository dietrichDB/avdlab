import type { ReactNode } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis, Area, AreaChart } from 'recharts'
import type { HostMetrics } from '../api/types'
import { useDarkMode } from '../hooks/useDarkMode'
import { containerRows, diskMounts, fmtTime, hostRows, latestDisk, shortContainer, topContainers } from '../lib/metrics'
import { fmtBytes } from '../lib/format'

// categorical slots (fixed order), light / dark steps
const SERIES_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7', '#e34948']
const SERIES_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']

function useTheme() {
  const dark = useDarkMode()
  return {
    series: dark ? SERIES_DARK : SERIES_LIGHT,
    grid: dark ? '#27272a' : '#e4e4e7',
    axis: dark ? '#a1a1aa' : '#71717a',
    tooltip: {
      contentStyle: { background: dark ? '#18181b' : '#ffffff', border: `1px solid ${dark ? '#3f3f46' : '#e4e4e7'}`, fontSize: 12, color: dark ? '#f4f4f5' : '#18181b' },
      labelStyle: { color: dark ? '#a1a1aa' : '#52525b' },
    },
  }
}

function ChartCard({ title, sub, children }: { title: string; sub?: ReactNode; children: ReactNode }) {
  return (
    <section className="card p-2">
      <div className="mb-1 flex items-baseline gap-2 px-1">
        <span className="h2">{title}</span>
        {sub && <span className="text-xs text-zinc-500">{sub}</span>}
      </div>
      <div className="h-48">{children}</div>
    </section>
  )
}

export default function HostCharts({ metrics }: { metrics: HostMetrics }) {
  const t = useTheme()
  const rows = hostRows(metrics.points)
  const mounts = diskMounts(metrics.points)
  const disks = latestDisk(metrics.points)
  const top = topContainers(metrics.containers, 10)
  const crow = containerRows(metrics.containers, top)
  const last = rows[rows.length - 1]
  const x = { dataKey: 't', type: 'number' as const, domain: ['dataMin', 'dataMax'] as [string, string], scale: 'time' as const, tickFormatter: fmtTime, stroke: t.axis, tick: { fontSize: 11 }, minTickGap: 40 }
  const y = { stroke: t.axis, tick: { fontSize: 11 }, width: 44 }
  const tip = { ...t.tooltip, labelFormatter: (v: unknown) => fmtTime(Number(v)) }
  const grid = <CartesianGrid stroke={t.grid} strokeDasharray="0" vertical={false} />
  if (!rows.length) return <div className="card p-6 text-center text-sm text-zinc-500">No samples yet: the sampler runs every 20 s while the host is watched.</div>
  return (
    <div className="grid gap-3 lg:grid-cols-2" data-testid="host-charts">
      <ChartCard title="CPU" sub={last?.cpu != null ? `${last.cpu}%` : undefined}>
        <ResponsiveContainer>
          <AreaChart data={rows}>
            {grid}
            <XAxis {...x} />
            <YAxis {...y} domain={[0, 100]} unit="%" />
            <Tooltip {...tip} formatter={(v) => [`${v}%`, 'CPU']} />
            <Area type="monotone" dataKey="cpu" name="CPU %" stroke={t.series[0]} fill={t.series[0]} fillOpacity={0.15} strokeWidth={2} isAnimationActive={false} connectNulls />
          </AreaChart>
        </ResponsiveContainer>
      </ChartCard>
      <ChartCard title="Load average" sub={last ? `${last.load1 ?? '—'} / ${last.load5 ?? '—'} / ${last.load15 ?? '—'}` : undefined}>
        <ResponsiveContainer>
          <LineChart data={rows}>
            {grid}
            <XAxis {...x} />
            <YAxis {...y} />
            <Tooltip {...tip} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Line type="monotone" dataKey="load1" name="1 min" stroke={t.series[0]} dot={false} strokeWidth={2} isAnimationActive={false} />
            <Line type="monotone" dataKey="load5" name="5 min" stroke={t.series[1]} dot={false} strokeWidth={2} isAnimationActive={false} />
            <Line type="monotone" dataKey="load15" name="15 min" stroke={t.series[2]} dot={false} strokeWidth={2} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </ChartCard>
      <ChartCard title="Memory" sub={last?.memTotal != null ? `${last.memUsed} / ${last.memTotal} GB used` : undefined}>
        <ResponsiveContainer>
          <AreaChart data={rows}>
            {grid}
            <XAxis {...x} />
            <YAxis {...y} unit=" G" domain={[0, last?.memTotal ?? 'auto']} />
            <Tooltip {...tip} formatter={(v, n) => [`${v} GB`, n]} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Area type="monotone" dataKey="memUsed" name="used" stackId="m" stroke={t.series[0]} fill={t.series[0]} fillOpacity={0.3} strokeWidth={2} isAnimationActive={false} />
            <Area type="monotone" dataKey="memAvail" name="available" stackId="m" stroke={t.series[2]} fill={t.series[2]} fillOpacity={0.12} strokeWidth={2} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </ChartCard>
      <ChartCard title="Disk used" sub={disks.map((d) => `${d.mount} ${fmtBytes(d.used)} / ${fmtBytes(d.size)}`).join(' · ')}>
        <ResponsiveContainer>
          <LineChart data={rows}>
            {grid}
            <XAxis {...x} />
            <YAxis {...y} domain={[0, 100]} unit="%" />
            <Tooltip {...tip} formatter={(v, n) => [`${v}%`, n]} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {mounts.map((m, i) => <Line key={m} type="monotone" dataKey={`disk:${m}`} name={m} stroke={t.series[i % t.series.length]} dot={false} strokeWidth={2} isAnimationActive={false} />)}
          </LineChart>
        </ResponsiveContainer>
      </ChartCard>
      <section className="card p-2 lg:col-span-2">
        <div className="mb-1 flex items-baseline gap-2 px-1">
          <span className="h2">Container memory</span>
          <span className="text-xs text-zinc-500">top {top.length} by current use (MB)</span>
        </div>
        <div className="h-64">
          {crow.length ? (
            <ResponsiveContainer>
              <LineChart data={crow}>
                {grid}
                <XAxis {...x} />
                <YAxis {...y} unit=" M" width={60} />
                <Tooltip {...tip} formatter={(v, n) => [`${v} MB`, shortContainer(String(n))]} itemSorter={(i) => -Number(i.value ?? 0)} />
                <Legend wrapperStyle={{ fontSize: 11 }} formatter={(v) => shortContainer(String(v))} />
                {top.map((name, i) => (
                  // 8 hues in fixed order; series 9-10 reuse slots 1-2 dashed (secondary encoding)
                  <Line key={name} type="monotone" dataKey={name} stroke={t.series[i % t.series.length]} strokeDasharray={i >= t.series.length ? '5 3' : undefined} dot={false} strokeWidth={2} isAnimationActive={false} connectNulls />
                ))}
              </LineChart>
            </ResponsiveContainer>
          ) : <div className="p-6 text-center text-sm text-zinc-500">No container samples.</div>}
        </div>
      </section>
    </div>
  )
}
