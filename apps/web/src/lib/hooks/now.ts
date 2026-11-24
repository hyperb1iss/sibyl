'use client';

import { useSyncExternalStore } from 'react';

/** How often relative times ("3m ago") move on an open page. */
export const NOW_TICK_MS = 30_000;

let current = Date.now();
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    // Nothing kept time while no one was watching.
    current = Date.now();
    timer = setInterval(() => {
      current = Date.now();
      for (const notify of listeners) notify();
    }, NOW_TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function snapshot(): number {
  return current;
}

/**
 * The current time, moving every `NOW_TICK_MS`. Every caller shares one
 * interval, so a page of a hundred relative timestamps runs one timer, and
 * the timer stops when the last of them unmounts.
 */
export function useNow(): number {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
