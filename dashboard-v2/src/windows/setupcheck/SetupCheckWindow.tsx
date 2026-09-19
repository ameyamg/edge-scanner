import { useMemo, useState } from 'react'
import type { CheckEvent, CheckNow, CheckRow, CheckStatus, SetupCheckConfig } from '../../types'
import { api } from '../../lib/api'
import { usePoll } from '../../lib/usePoll'
import { fmtPrice } from '../../lib/format'
import { fmtTimeET } from '../../lib/time'
import { useLinkedSymbol, linkSymbol } from '../../stores/linkStore'
import { useScreens } from '../../stores/screensStore'
import { Empty, SymbolInput } from '../../components/primitives'

/** Setup check: type a stock, see for every setup whether its alert went out in
 *  the last few minutes, or exactly what stopped it. The answer to "why did I not
 *  get an alert on X?", which the feed cannot give because a blocked alert never
 *  reaches it. The scanner records as it runs (scanner/recent_activity.py). */

const STATUS: Record<CheckStatus, { label: string; cls: string; title: string }> = {
  sent: { label: 'Sent', cls: 'long', title: 'The alert went out to the feed' },
  blocked: { label: 'Blocked', cls: 'short', title: 'The alert pattern happened, but a universe filter or parameter stopped it' },
  waiting: { label: 'Waiting', cls: 'sc-wait', title: 'Part of an "at least N of" setup fired; still waiting for the rest' },
  repeat: { label: 'Held', cls: 'muted', title: "Held back by a don't-repeat timer: it already alerted on this stock recently" },
  suppressed: { label: 'Suppressed', cls: 'muted', title: 'Replaced by a stronger setup on the same bar' },
  quiet: { label: 'No pattern', cls: 'muted', title: 'Its alert pattern did not happen in this window' },
}

// Event timestamps are the bar's START; the Scanner window shows the close.
const closeTime = (epoch: number) => fmtTimeET(new Date((epoch + 60) * 1000).toISOString())
const dirLabel = (d: string) => (d === 'long' ? 'long' : d === 'short' ? 'short' : '')

/** One line for a row with no events: per direction, pass or the first reason. */
const nowSummary = (now: CheckNow[]) => now.map(n => {
  const d = dirLabel(n.direction)
  const what = n.ok === true ? 'filters pass' : (n.reasons[0] ?? '')
  return d ? `${d}: ${what}` : what
}).join(' · ')

function NowLine({ n, quietRow }: { n: CheckNow; quietRow: boolean }) {
  const d = dirLabel(n.direction)
  const text = n.ok === true
    ? (quietRow ? 'filters pass, waiting for its alert pattern' : 'filters pass now')
    : n.reasons.join('; ')
  return (
    <div className="sc-now">
      <span className={n.ok === true ? 'up' : n.ok === false ? 'down' : 'faint'}>{n.ok === true ? '✓' : n.ok === false ? '✗' : '·'}</span>
      {d && <span className="faint sc-dir">{d}</span>}
      <span className={n.ok === false ? 'dim' : 'faint'}>{text}</span>
    </div>
  )
}

function EventLine({ e }: { e: CheckEvent }) {
  const st = STATUS[e.outcome]
  return (
    <div className="sc-ev">
      <span className="mono faint">{closeTime(e.ts)}</span>
      <span className={`badge ${st.cls}`} title={st.title}>{st.label}</span>
      {e.direction && <span className="faint sc-dir">{dirLabel(e.direction)}</span>}
      <span className="dim">{e.reasons.length ? e.reasons.join('; ') : (e.trigger || 'alert sent')}</span>
      {e.reasons.length > 0 && e.trigger && <span className="faint ellipsis" title={e.trigger}>{e.trigger}</span>}
    </div>
  )
}

function Row({ r, open, onToggle }: { r: CheckRow; open: boolean; onToggle(): void }) {
  const st = STATUS[r.status]
  const last = r.events[0]
  const quiet = r.status === 'quiet'
  return (
    <div className={`sc-row${open ? ' open' : ''}`}>
      <button className="sc-head" onClick={onToggle} title={quiet ? 'Show what would block it now' : 'Show every event and the state now'}>
        <span className={`badge ${st.cls}`} title={st.title}>{st.label}</span>
        <span className="badge muted sc-src">{r.source === 'CS' || r.source === 'custom' ? 'CS' : 'SYS'}</span>
        <span className="sc-name ellipsis">{r.name}</span>
        {last ? (
          <span className="faint mono sc-when">{closeTime(last.ts)}{last.direction ? ` ${dirLabel(last.direction)}` : ''}{r.events.length > 1 ? ` ×${r.events.length}` : ''}</span>
        ) : (
          <span className={`sc-when ${r.now.some(n => n.ok === true) ? 'up' : r.now.some(n => n.ok === false) ? 'down' : 'faint'}`}>
            {r.now.some(n => n.ok === true) ? 'could fire' : r.now.some(n => n.ok === false) ? 'blocked now' : 'waiting'}
          </span>
        )}
      </button>
      {!open && last && last.reasons.length > 0 && <div className="sc-why dim ellipsis" title={last.reasons.join('\n')}>{last.reasons.join('; ')}</div>}
      {!open && !last && r.now.length > 0 && <div className="sc-why faint ellipsis" title={nowSummary(r.now)}>{nowSummary(r.now)}</div>}
      {open && (
        <div className="sc-body">
          {r.events.map((e, i) => <EventLine key={i} e={e} />)}
          {r.now.length > 0 && <div className="sc-sub faint">If it fired on the latest bar</div>}
          {r.now.map((n, i) => <NowLine key={i} n={n} quietRow={quiet} />)}
        </div>
      )}
    </div>
  )
}

export function SetupCheckWindow({ win }: { win: SetupCheckConfig }) {
  const symbol = useLinkedSymbol(win, win.symbol)
  const minutes = win.minutes || 5
  const { data, error, loading, refresh } = usePoll(() => api.setupCheck(symbol!, minutes), 15000, !!symbol, [symbol, minutes])
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [showQuiet, setShowQuiet] = useState(false)
  const toggle = (id: string) => setOpen(o => ({ ...o, [id]: !o[id] }))

  const rows = useMemo(() => (data?.symbol === symbol ? data.setups : []), [data, symbol])
  const active = rows.filter(r => r.status !== 'quiet')
  const quiet = rows.filter(r => r.status === 'quiet')
  const counts = active.reduce<Record<string, number>>((m, r) => ({ ...m, [r.status]: (m[r.status] ?? 0) + 1 }), {})

  const header = (
    <div className="sc-top">
      <SymbolInput value={symbol} small onCommit={sym => linkSymbol(win, sym)} />
      <select className="input sm" value={minutes} title="How far back to look"
        onChange={e => useScreens.getState().updateWindow(win.id, { minutes: Number(e.target.value) })}>
        {[5, 10, 15].map(m => <option key={m} value={m}>last {m} min</option>)}
      </select>
      {data?.found && <span className="faint mono" style={{ fontSize: 11 }}>{fmtPrice(data.price)}{data.last_bar ? ` · bar ${closeTime(data.last_bar)}` : ''}</span>}
      <span className="flex-spacer" />
      <button className="btn sm" onClick={() => void refresh()} disabled={loading} title="Check again now (refreshes every 15 s)">{loading ? '…' : 'Refresh'}</button>
    </div>
  )

  if (!symbol) return <div className="sc-wrap">{header}<Empty title="No symbol">Type a stock, or pick a link color and click a symbol in another window.</Empty></div>
  if (error && !data) return <div className="sc-wrap">{header}<div className="wf-error">{error}</div></div>
  if (data && data.symbol === symbol && !data.found) return <div className="sc-wrap">{header}<Empty title={`${symbol} is not scanned`}>{data.message}</Empty></div>

  return (
    <div className="sc-wrap">
      {header}
      <div className="sc-scroll">
        <div className="sc-sum">
          {(['sent', 'blocked', 'waiting', 'repeat', 'suppressed'] as CheckStatus[]).filter(s => counts[s]).map(s => (
            <span key={s} className={`badge ${STATUS[s].cls}`} title={STATUS[s].title}>{counts[s]} {STATUS[s].label.toLowerCase()}</span>
          ))}
          {!active.length && data && <span className="faint">No setup's alert pattern happened on {symbol} in the last {minutes} min.</span>}
        </div>
        {active.map(r => <Row key={r.id} r={r} open={!!open[r.id]} onToggle={() => toggle(r.id)} />)}
        {quiet.length > 0 && (
          <button className="sc-quiet-toggle faint" onClick={() => setShowQuiet(v => !v)}>
            {showQuiet ? '▾' : '▸'} No alert pattern in the last {minutes} min ({quiet.length}): what would block each one now
          </button>
        )}
        {showQuiet && quiet.map(r => <Row key={r.id} r={r} open={!!open[r.id]} onToggle={() => toggle(r.id)} />)}
      </div>
    </div>
  )
}
