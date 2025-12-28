'use client';

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useSyncExternalStore,
} from 'react';

// Theme options
export type Theme = 'neon' | 'dawn';
export type ThemePreference = Theme | 'system';

const STORAGE_KEY = 'sibyl-theme';

interface ThemeContextValue {
  theme: Theme; // The resolved theme (never 'system')
  preference: ThemePreference; // What the user selected
  setPreference: (pref: ThemePreference) => void;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function getSystemTheme(): Theme {
  if (typeof window === 'undefined') return 'neon';
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'dawn' : 'neon';
}

function resolveTheme(preference: ThemePreference): Theme {
  if (preference === 'system') {
    return getSystemTheme();
  }
  return preference;
}

// Storage subscription for useSyncExternalStore
let listeners: Array<() => void> = [];

function subscribeToStorage(callback: () => void) {
  listeners.push(callback);

  // Also listen for storage events (cross-tab sync)
  const handleStorage = (e: StorageEvent) => {
    if (e.key === STORAGE_KEY) callback();
  };
  window.addEventListener('storage', handleStorage);

  return () => {
    listeners = listeners.filter(l => l !== callback);
    window.removeEventListener('storage', handleStorage);
  };
}

function getStorageSnapshot(): ThemePreference {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === 'neon' || stored === 'dawn' || stored === 'system') {
    return stored;
  }
  return 'system';
}

function getServerSnapshot(): ThemePreference {
  return 'system';
}

function setStoredPreference(pref: ThemePreference) {
  localStorage.setItem(STORAGE_KEY, pref);
  // Notify all listeners
  for (const listener of listeners) {
    listener();
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  // Use useSyncExternalStore for proper hydration-safe localStorage reading
  const preference = useSyncExternalStore(
    subscribeToStorage,
    getStorageSnapshot,
    getServerSnapshot
  );

  const theme = resolveTheme(preference);

  // Apply theme to document
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  // Listen for system preference changes
  useEffect(() => {
    if (preference !== 'system') return;

    const mediaQuery = window.matchMedia('(prefers-color-scheme: light)');
    const handler = () => {
      // Force re-render to pick up new system theme
      for (const listener of listeners) {
        listener();
      }
    };

    mediaQuery.addEventListener('change', handler);
    return () => mediaQuery.removeEventListener('change', handler);
  }, [preference]);

  const setPreference = useCallback((pref: ThemePreference) => {
    setStoredPreference(pref);
  }, []);

  const toggleTheme = useCallback(() => {
    // Cycle: neon -> dawn -> system -> neon
    const current = getStorageSnapshot();
    const next: ThemePreference =
      current === 'neon' ? 'dawn' : current === 'dawn' ? 'system' : 'neon';
    setStoredPreference(next);
  }, []);

  const value = useMemo(
    () => ({ theme, preference, setPreference, toggleTheme }),
    [theme, preference, setPreference, toggleTheme]
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error('useTheme must be used within a ThemeProvider');
  }
  return context;
}
