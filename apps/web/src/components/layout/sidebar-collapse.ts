'use client';

import { useCallback, useEffect, useLayoutEffect, useState } from 'react';

/**
 * Desktop sidebar collapse state.
 *
 * The module-level boolean below is the source of truth. localStorage
 * (`sibyl-sidebar-collapsed`) is a write-through copy so the rail survives
 * reloads; a missing or throwing storage degrades to a session-only rail
 * rather than a dead toggle. The root layout's inline bootstrap script reads
 * the same key before React runs and stamps `data-sidebar="collapsed"` on
 * <html>; a CSS rule pins the rail to its collapsed width until
 * `data-sidebar-hydrated` lands, which is how the first paint avoids a 256px
 * to 64px jump.
 */
export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'sibyl-sidebar-collapsed';
export const SIDEBAR_EXPANDED_WIDTH = 256;
export const SIDEBAR_COLLAPSED_WIDTH = 64;

let collapsedState = false;
let listeners: Array<() => void> = [];

// window.localStorage rather than the bare global: Node 24 ships an
// experimental `localStorage` global that shadows jsdom's in tests.
// Returns undefined when storage cannot be read at all.
function readStoredCollapsed(): boolean | undefined {
  try {
    if (typeof window === 'undefined') return undefined;
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === 'true';
  } catch {
    return undefined;
  }
}

function writeStoredCollapsed(collapsed: boolean) {
  try {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(collapsed));
    }
  } catch {
    // Storage can be unavailable (private mode, quota, hardened profiles).
    // The in-memory value still drives the rail for this session.
  }
}

function stampDocument(collapsed: boolean) {
  if (typeof document === 'undefined') return;
  if (collapsed) {
    document.documentElement.setAttribute('data-sidebar', 'collapsed');
  } else {
    document.documentElement.removeAttribute('data-sidebar');
  }
}

function notify() {
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void) {
  listeners.push(listener);

  // Another tab changed the preference: adopt it without writing it back.
  const handleStorage = (event: StorageEvent) => {
    if (event.key !== SIDEBAR_COLLAPSED_STORAGE_KEY) return;
    collapsedState = event.newValue === 'true';
    stampDocument(collapsedState);
    listener();
  };
  window.addEventListener('storage', handleStorage);

  return () => {
    listeners = listeners.filter(l => l !== listener);
    window.removeEventListener('storage', handleStorage);
  };
}

export function getSidebarCollapsed(): boolean {
  return collapsedState;
}

/** Adopt the stored preference (page load). Unreadable storage keeps the current value. */
export function syncSidebarCollapsedFromStorage(): boolean {
  const stored = readStoredCollapsed();
  if (stored !== undefined) {
    collapsedState = stored;
  }
  return collapsedState;
}

export function setSidebarCollapsed(collapsed: boolean) {
  collapsedState = collapsed;
  writeStoredCollapsed(collapsed);
  stampDocument(collapsed);
  notify();
}

export function toggleSidebarCollapsed() {
  setSidebarCollapsed(!collapsedState);
}

/**
 * Read the collapse state without a hydration mismatch: the server and the
 * hydration pass render expanded, then a layout effect swaps in the stored
 * value before the browser paints and releases the CSS width pin.
 */
export function useSidebarCollapsed() {
  const [collapsed, setCollapsedState] = useState(false);
  const [settled, setSettled] = useState(false);

  useLayoutEffect(() => {
    setCollapsedState(syncSidebarCollapsedFromStorage());
    document.documentElement.setAttribute('data-sidebar-hydrated', '');
    return subscribe(() => setCollapsedState(collapsedState));
  }, []);

  // Width transitions stay off until the first frame has painted, so the
  // stored state lands instantly instead of animating in on page load.
  useEffect(() => {
    const frame = requestAnimationFrame(() => setSettled(true));
    return () => cancelAnimationFrame(frame);
  }, []);

  const setCollapsed = useCallback((next: boolean) => setSidebarCollapsed(next), []);
  const toggle = useCallback(() => toggleSidebarCollapsed(), []);

  return { collapsed, setCollapsed, toggle, animate: settled };
}
