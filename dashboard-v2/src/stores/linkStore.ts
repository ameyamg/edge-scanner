import { create } from 'zustand'
import type { LinkColor, WindowBase } from '../types'
import { useScreens } from './screensStore'

type Group = Exclude<LinkColor, 'none'>

interface LinkState {
  symbols: Record<Group, string | null>
  setSymbol(color: Group, sym: string | null): void
}

export const useLinks = create<LinkState>()(set => ({
  symbols: { red: null, green: null, blue: null, yellow: null, purple: null },
  setSymbol(color, sym) {
    set(s => ({ symbols: { ...s.symbols, [color]: sym ? sym.toUpperCase() : null } }))
  },
}))

/** The symbol a window should show: its link group's symbol; while the group has
 *  had no click yet (or the window is unlinked) its own configured symbol. */
export function useLinkedSymbol(win: WindowBase, fallback: string | null): string | null {
  const linked = useLinks(s => (win.link === 'none' ? null : s.symbols[win.link]))
  return win.link === 'none' ? fallback : (linked ?? fallback)
}

/** A window "publishes" a symbol: drives its link group, or (when unlinked) just its own config. */
export function linkSymbol(win: WindowBase, sym: string, ownKey: 'symbol' | null = 'symbol') {
  const s = sym.toUpperCase()
  if (win.link !== 'none') {
    useLinks.getState().setSymbol(win.link, s)
  } else if (ownKey) {
    useScreens.getState().updateWindow(win.id, { [ownKey]: s } as Record<string, unknown>)
  }
}
