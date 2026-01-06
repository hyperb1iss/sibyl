/**
 * Client-side preferences storage with localStorage persistence.
 *
 * Provides a consistent pattern for storing UI preferences that should
 * persist across sessions (filters, sort orders, view modes, etc.)
 */

import { useCallback, useEffect, useRef, useState } from 'react';

const STORAGE_PREFIX = 'sibyl:';

// =============================================================================
// Low-level storage utilities
// =============================================================================

function getStorageKey(key: string): string {
  return `${STORAGE_PREFIX}${key}`;
}

function isClient(): boolean {
  return typeof window !== 'undefined';
}

/**
 * Read a value from localStorage, returning undefined if not found or on error.
 */
export function readStorage<T>(key: string): T | undefined {
  if (!isClient()) return undefined;
  try {
    const raw = localStorage.getItem(getStorageKey(key));
    return raw ? (JSON.parse(raw) as T) : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Write a value to localStorage. Pass undefined to remove the key.
 */
export function writeStorage<T>(key: string, value: T | undefined): void {
  if (!isClient()) return;
  try {
    const fullKey = getStorageKey(key);
    if (value === undefined) {
      localStorage.removeItem(fullKey);
    } else {
      localStorage.setItem(fullKey, JSON.stringify(value));
    }
  } catch {
    // Ignore storage errors (quota exceeded, private browsing, etc.)
  }
}

// =============================================================================
// React hooks
// =============================================================================

interface UseClientPrefsOptions<T> {
  /** Storage key (will be prefixed with 'sibyl:') */
  key: string;
  /** Default value when nothing is stored */
  defaultValue: T;
  /** Optional: validate/migrate stored data */
  validate?: (stored: unknown) => T | undefined;
}

/**
 * Hook for persisting client-side preferences to localStorage.
 *
 * @example
 * ```tsx
 * const [prefs, setPrefs] = useClientPrefs({
 *   key: 'epics:filters',
 *   defaultValue: { sort: 'updated_desc', statuses: [] },
 * });
 *
 * // Update a single field
 * setPrefs(prev => ({ ...prev, sort: 'name_asc' }));
 *
 * // Or replace entirely
 * setPrefs({ sort: 'priority', statuses: ['planning'] });
 * ```
 */
export function useClientPrefs<T>({
  key,
  defaultValue,
  validate,
}: UseClientPrefsOptions<T>): [T, (update: T | ((prev: T) => T)) => void] {
  // Use ref to track if we've hydrated from storage
  const hasHydrated = useRef(false);

  const [value, setValue] = useState<T>(() => {
    // SSR: return default
    if (!isClient()) return defaultValue;

    // Client: try to restore from storage
    const stored = readStorage<unknown>(key);
    if (stored !== undefined) {
      if (validate) {
        const validated = validate(stored);
        return validated !== undefined ? validated : defaultValue;
      }
      return stored as T;
    }
    return defaultValue;
  });

  // Mark as hydrated after first render
  useEffect(() => {
    hasHydrated.current = true;
  }, []);

  // Persist to storage when value changes (after hydration)
  useEffect(() => {
    if (!hasHydrated.current) return;

    // Don't store if it equals the default (keeps storage clean)
    if (JSON.stringify(value) === JSON.stringify(defaultValue)) {
      writeStorage(key, undefined);
    } else {
      writeStorage(key, value);
    }
  }, [key, value, defaultValue]);

  // Stable setter that handles both direct values and updater functions
  const setValueStable = useCallback((update: T | ((prev: T) => T)) => {
    setValue(prev => (typeof update === 'function' ? (update as (prev: T) => T)(prev) : update));
  }, []);

  return [value, setValueStable];
}

// =============================================================================
// URL + Storage sync hook (for filters that should be shareable via URL)
// =============================================================================

interface UseUrlPrefsOptions<T> {
  /** Storage key for localStorage persistence */
  storageKey: string;
  /** Default value when nothing is stored or in URL */
  defaultValue: T;
  /** Convert URL search params to prefs object */
  fromParams: (params: URLSearchParams) => Partial<T>;
  /** Convert prefs object to URL search params (return undefined to omit) */
  toParams: (prefs: T, defaults: T) => Record<string, string | undefined>;
  /** Optional: validate stored data */
  validate?: (stored: unknown) => T | undefined;
}

/**
 * Hook for preferences that sync between URL and localStorage.
 *
 * - URL takes precedence over localStorage
 * - Changes update both URL and storage
 * - Navigating to page with no params restores from storage
 *
 * @example
 * ```tsx
 * const { prefs, setPrefs, updateUrl } = useUrlPrefs({
 *   storageKey: 'epics:filters',
 *   defaultValue: { sort: 'updated_desc', statuses: [] as string[] },
 *   fromParams: (params) => ({
 *     sort: params.get('sort') || undefined,
 *     statuses: params.get('status')?.split(',') || undefined,
 *   }),
 *   toParams: (prefs, defaults) => ({
 *     sort: prefs.sort !== defaults.sort ? prefs.sort : undefined,
 *     status: prefs.statuses.length ? prefs.statuses.join(',') : undefined,
 *   }),
 * });
 * ```
 */
export function useUrlPrefs<T extends Record<string, unknown>>({
  storageKey,
  defaultValue,
  fromParams,
  toParams,
  validate,
}: UseUrlPrefsOptions<T>): {
  prefs: T;
  setPrefs: (update: Partial<T> | ((prev: T) => T)) => void;
  updateUrl: (router: { push: (url: string) => void }, basePath: string) => void;
  hasUrlParams: boolean;
} {
  const [prefs, setPrefsInternal] = useClientPrefs<T>({
    key: storageKey,
    defaultValue,
    validate,
  });

  const [hasUrlParams, setHasUrlParams] = useState(false);
  const [urlInitialized, setUrlInitialized] = useState(false);

  // On mount, check if URL has params and apply them
  useEffect(() => {
    if (urlInitialized || !isClient()) return;
    setUrlInitialized(true);

    const params = new URLSearchParams(window.location.search);
    const fromUrl = fromParams(params);
    const hasParams = Object.values(fromUrl).some(v => v !== undefined);

    setHasUrlParams(hasParams);

    if (hasParams) {
      // URL has params - merge with defaults and use those
      setPrefsInternal(prev => ({ ...prev, ...fromUrl }));
    }
    // If no URL params, stored prefs are already loaded by useClientPrefs
  }, [urlInitialized, fromParams, setPrefsInternal]);

  const setPrefs = useCallback(
    (update: Partial<T> | ((prev: T) => T)) => {
      setPrefsInternal(prev => {
        if (typeof update === 'function') {
          return update(prev);
        }
        return { ...prev, ...update };
      });
    },
    [setPrefsInternal]
  );

  const updateUrl = useCallback(
    (router: { push: (url: string) => void }, basePath: string) => {
      const paramObj = toParams(prefs, defaultValue);
      const params = new URLSearchParams();

      for (const [key, value] of Object.entries(paramObj)) {
        if (value !== undefined) {
          params.set(key, value);
        }
      }

      const query = params.toString();
      router.push(query ? `${basePath}?${query}` : basePath);
    },
    [prefs, defaultValue, toParams]
  );

  return { prefs, setPrefs, updateUrl, hasUrlParams };
}
