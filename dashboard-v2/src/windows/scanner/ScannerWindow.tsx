import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import type { Alert, FeedId, ScannerConfig, ToneName } from '../../types'
import { useFeeds, visibleSources, SOURCE_LABEL, alertKey } from '../../stores/feedsStore'
import { useCapabilities } from '../../stores/capabilitiesStore'
import { useSettings } from '../../stores/settingsStore'
import { useSetups, useSystemSetups } from '../../stores/setupsStore'
import { useScreens } from '../../stores/screensStore'
import { linkSymbol } from '../../stores/linkStore'
import { VirtualTable } from '../../components/VirtualTable'
import { ChipMultiSelect, ColumnPicker, Field, Select, Toggle } from '../../components/primitives'
import { playTone, speak, spellSymbol } from '../../lib/audio'
import { SCANNER_COLUMNS, SCANNER_COLUMN_LIST, filterAlerts } from './columns'

function ttsText(a: Alert): string {
  const what = a.setup_label || (a.setup ? useSetups.getState().label(a.setup) : '') || a.trigger.replace(/_/g, ' ')
  return `${spellSymbol(a.symbol)} ${a.direction} ${what}`
}

export function ScannerWindow({ win }: { win: ScannerConfig }) {
  const alerts = useFeeds(s => s.alerts)
  const status = useFeeds(s => s.status)
  const lastSeq = useFeeds(s => s.lastSeq)
  const latest = useFeeds(s => s.latest)
  const globalMute = useSettings(s => s.globalMute)
  const hasSystem = useCapabilities(s => s.system_setups)
  const rows = useMemo(() => filterAlerts(alerts, win, hasSystem), [alerts, win, hasSystem])
  const columns = useMemo(() => win.columns.map(id => SCANNER_COLUMNS[id]).filter(Boolean), [win.columns])
  const mountSeq = useRef(lastSeq)
  const seenSeq = useRef(lastSeq)
  const newKeys = useRef(new Set<string>())

  // sound / TTS on a new alert that passes this window's filter
  useEffect(() => {
    if (lastSeq === seenSeq.current || !latest) return
    seenSeq.current = lastSeq
    if (filterAlerts([latest], win, hasSystem).length === 0) return
    newKeys.current.add(alertKey(latest))
    setTimeout(() => newKeys.current.delete(alertKey(latest)), 2000)
    if (win.muted || globalMute) return
    if (win.sound.tone !== 'off') playTone(win.sound.tone)
    if (win.sound.tts) speak(ttsText(latest), latest.symbol)
  }, [lastSeq, latest, win, globalMute, hasSystem])

  const shownSources = win.sources.filter(s => visibleSources(hasSystem).includes(s))
  const what = shownSources.length ? shownSources.map(s => SOURCE_LABEL[s]).join(' + ') : 'any'
  const empty: ReactNode = status === 'connected'
    ? (alerts.length ? 'No alerts match the filters' : `No ${what} alerts yet today`)
    : <span className="pulse">Connecting to the scanner feed…</span>

  return (
    <VirtualTable<Alert>
      rows={rows}
      columns={columns}
      rowKey={alertKey}
      onRowClick={a => linkSymbol(win, a.symbol, null)}
      rowClass={a => `${win.rowTint ? (a.direction === 'short' ? 'tint-short' : 'tint-long') : ''}${newKeys.current.has(alertKey(a)) && lastSeq > mountSeq.current ? ' row-new' : ''}`}
      emptyText={empty}
      colWidths={win.colWidths}
      onColWidths={colWidths => useScreens.getState().updateWindow(win.id, { colWidths })}
    />
  )
}

export function ScannerSettings({ win, onChange }: { win: ScannerConfig; onChange(p: Partial<ScannerConfig>): void }) {
  const counts = useFeeds(s => s.counts)
  const customSetups = useSetups(s => s.custom)
  const systemSetups = useSystemSetups()
  const hasSystem = useCapabilities(s => s.system_setups)
  const setupOptions = useMemo(() => [
    ...systemSetups.map(s => ({ value: s.code, label: s.name || s.code })),
    ...customSetups.map(c => ({ value: c.id, label: `${c.name}${c.enabled ? '' : ' (off)'}` })),
  ].sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' })), [customSetups, systemSetups])
  const sources = visibleSources(hasSystem)
  const sourceOptions = sources.map(s => ({ value: s, label: `${SOURCE_LABEL[s]} (${counts[s]})` }))
  return (
    <>
      {/* One source only (no built-in setups on this backend): nothing to pick. */}
      {sources.length > 1 && (
        <Field label="Sources (empty = all)">
          <ChipMultiSelect value={win.sources.filter(s => sources.includes(s))} onChange={next => onChange({ sources: next as FeedId[] })} options={sourceOptions} />
        </Field>
      )}
      <div className="grid2">
        <Field label="Direction">
          <Select value={win.direction} onChange={direction => onChange({ direction })} options={[{ value: 'all', label: 'All' }, { value: 'long', label: 'Long' }, { value: 'short', label: 'Short' }]} />
        </Field>
        <Field label="Min score"><input className="input" type="number" min={0} max={100} value={win.minScore} onChange={e => onChange({ minScore: Number(e.target.value) || 0 })} /></Field>
        <Field label="Symbol filter"><input className="input mono" style={{ textTransform: 'uppercase' }} value={win.symbolFilter} placeholder="prefix" onChange={e => onChange({ symbolFilter: e.target.value })} /></Field>
        <Field label="Max rows"><input className="input" type="number" min={10} max={5000} value={win.maxRows} onChange={e => onChange({ maxRows: Math.max(10, Number(e.target.value) || 500) })} /></Field>
      </div>
      <Field label="Setups"><ChipMultiSelect value={win.setups} onChange={setups => onChange({ setups })} options={setupOptions} /></Field>
      <Field label="Columns"><ColumnPicker value={win.columns} onChange={columns => onChange({ columns })} all={SCANNER_COLUMN_LIST} /></Field>
      <div className="grid2">
        <Field label="Sound">
          <Select<ToneName> value={win.sound.tone} onChange={tone => { onChange({ sound: { ...win.sound, tone } }); playTone(tone) }}
            options={[{ value: 'off', label: 'Off' }, { value: 'ping', label: 'Ping' }, { value: 'chime', label: 'Chime' }, { value: 'buzz', label: 'Buzz' }]} />
        </Field>
        <Field label="Options">
          <Toggle checked={win.sound.tts} onChange={tts => onChange({ sound: { ...win.sound, tts } })} label="Speak alerts (TTS)" />
          <Toggle checked={win.rowTint} onChange={rowTint => onChange({ rowTint })} label="Tint rows by direction" />
        </Field>
      </div>
    </>
  )
}
