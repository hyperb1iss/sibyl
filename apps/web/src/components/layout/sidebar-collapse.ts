'use client';

import { useCallback, useEffect, useLayoutEffect, useState } from 'react';

/**
 * Desktop sidebar collapse state.
 *
 * Persisted under `sibyl-sidebar-collapsed` so the rail survives reloads.
 * The root layout's inline bootstrap script reads the same key before React
 * runs and stamps `data-sidebar="collapsed"` on <html>; a CSS rule pins the
 * rail to its collapsed width until `data-sidebar-hydrated` lands, which is
 * how the first paint avoids a 256px to 64px jump.
 */
export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'sibyl-sidebar-collapsed';
export const SIDEBAR_EXPANDED_WIDTH = 256;
export const SIDEBAR_COLLAPSED_WIDTH = 64;

let listeners: Array<() => void> = [];

// window.localStorage rather than the bare global: Node 24 ships an
// experimental `localStorage` global that shadows jsdom's in tests.
function readStoredCollapsed(): boolean {
  try {
    if (typeof window === 'undefined') return false;
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

function notify() {
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void) {
  listeners.push(listener);

  const handleStorage = (event: StorageEvent) => {
    if (event.key === SIDEBAR_COLLAPSED_STORAGE_KEY) listener();
  };
  window.addEventListener('storage', handleStorage);

  return () => {
    listeners = listeners.filter(l => l !== listener);
    window.removeEventListener('storage', handleStorage);
  };
}

export function setSidebarCollapsed(collapsed: boolean) {
  try {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(collapsed));
    }
  } catch {
    // Storage can be unavailable (private mode, quota); the in-memory state still updates.
  }
  if (typeof document !== 'undefined') {
    if (collapsed) {
      document.documentElement.setAttribute('data-sidebar', 'collapsed');
    } else {
      document.documentElement.removeAttribute('data-sidebar');
    }
  }
  notify();
}

export function toggleSidebarCollapsed() {
  setSidebarCollapsed(!readStoredCollapsed());
}

/**
 * Read the persisted collapse state without a hydration mismatch: the server
 * and the hydration pass render expanded, then a layout effect swaps in the
 * stored value before the browser paints.
 */
export function useSidebarCollapsed() {
  const [collapsed, setCollapsedState] = useState(false);
  const [settled, setSettled] = useState(false);

  useLayoutEffect(() => {
    setCollapsedState(readStoredCollapsed());
    document.documentElement.setAttribute('data-sidebar-hydrated', '');
    return subscribe(() => setCollapsedState(readStoredCollapsed()));
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
