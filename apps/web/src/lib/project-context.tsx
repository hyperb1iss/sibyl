'use client';

import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useProjects } from '@/lib/hooks/work-items';

const STORAGE_KEY = 'sibyl-project-context';
/** URL value that asks for every project on purpose: `?projects=all`. */
export const ALL_PROJECTS_PARAM = 'all';

/**
 * A selection is one of three things: not chosen yet, every project on
 * purpose, or a list of project IDs. Before this split, an empty list meant
 * "all projects" and was also the state a first visit started in, so every
 * page opened across every project the viewer could read.
 */
type ProjectSelection = { kind: 'unset' } | { kind: 'all' } | { kind: 'projects'; ids: string[] };

const UNSET: ProjectSelection = { kind: 'unset' };
const ALL: ProjectSelection = { kind: 'all' };

function projectsSelection(ids: string[]): ProjectSelection {
  return ids.length > 0 ? { kind: 'projects', ids } : ALL;
}

/** Read a stored selection. Legacy empty lists count as unset, not as all. */
export function parseStoredSelection(raw: string | null): ProjectSelection {
  if (!raw) return UNSET;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      const ids = parsed.filter((id): id is string => typeof id === 'string' && id.length > 0);
      return ids.length > 0 ? { kind: 'projects', ids } : UNSET;
    }
    if (parsed && typeof parsed === 'object') {
      const record = parsed as { mode?: unknown; projects?: unknown };
      if (record.mode === 'all') return ALL;
      if (Array.isArray(record.projects)) {
        const ids = record.projects.filter(
          (id): id is string => typeof id === 'string' && id.length > 0
        );
        if (ids.length > 0) return { kind: 'projects', ids };
      }
    }
  } catch {
    // Ignore parse errors
  }
  return UNSET;
}

function serializeSelection(selection: ProjectSelection): string | null {
  if (selection.kind === 'unset') return null;
  if (selection.kind === 'all') return JSON.stringify({ mode: 'all' });
  return JSON.stringify({ projects: selection.ids });
}

/** The project with the latest activity, the one a fresh visit opens on. */
export function mostRecentProjectId(
  projects: Array<{ id: string; metadata?: Record<string, unknown> | null }>
): string | null {
  let best: { id: string; time: number } | null = null;
  for (const project of projects) {
    const activity = project.metadata?.last_activity_at || project.metadata?.updated_at;
    const time = typeof activity === 'string' ? new Date(activity).getTime() || 0 : 0;
    if (!best || time > best.time) best = { id: project.id, time };
  }
  return best?.id ?? null;
}

// Pages that should always show all projects (no filtering)
const CROSS_PROJECT_PATHS = ['/projects', '/sources', '/settings'];

/**
 * Read the project selection from a URL's `projects` parameter, a
 * comma-separated list of project IDs. Links that scope a page to projects
 * build this parameter with `withProjectsContext`.
 */
export function parseProjectsParam(params: URLSearchParams): string[] {
  const raw = params.get('projects') ?? '';
  if (raw === ALL_PROJECTS_PARAM) return [];
  return raw.split(',').filter(Boolean);
}

function selectionFromParams(params: URLSearchParams): ProjectSelection {
  const raw = params.get('projects');
  if (raw === null || raw === '') return UNSET;
  if (raw === ALL_PROJECTS_PARAM) return ALL;
  return projectsSelection(raw.split(',').filter(Boolean));
}

interface ProjectContextValue {
  /** Selected project IDs. Empty array means "all projects" */
  selectedProjects: string[];
  /** Whether "all projects" mode is active */
  isAll: boolean;
  /** Toggle a single project in/out of selection */
  toggleProject: (projectId: string) => void;
  /** Set specific projects (replaces current selection) */
  setProjects: (projectIds: string[]) => void;
  /** Select a single project (convenience method) */
  selectProject: (projectId: string) => void;
  /** Clear selection (back to "all") */
  clearProjects: () => void;
  /** Whether this page respects project context */
  contextEnabled: boolean;
  /** False until a selection exists; pages wait rather than read every project */
  scopeReady: boolean;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectContextProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Check if current page should show all projects
  const contextEnabled = !CROSS_PROJECT_PATHS.some(path => pathname.startsWith(path));

  // Track whether we've completed initial hydration
  const isHydrated = useRef(false);
  const prevProjectsRef = useRef<string[] | null>(null);

  // Start unset; the real value lands in an effect after hydration. Hydration
  // is state, not only a ref, so the default and its fetch wait for the render
  // that already carries a stored choice instead of racing it.
  const [selection, setSelection] = useState<ProjectSelection>(UNSET);
  const [hydrated, setHydrated] = useState(false);

  const selectedProjects = useMemo(
    () => (selection.kind === 'projects' ? selection.ids : []),
    [selection]
  );
  const isAll = selection.kind === 'all';
  const scopeReady = !contextEnabled || (hydrated && selection.kind !== 'unset');

  // Initial hydration: sync from URL (primary) or localStorage (fallback)
  // This runs once after mount to ensure searchParams is available
  useEffect(() => {
    if (isHydrated.current) return;
    isHydrated.current = true;

    // URL is source of truth
    const fromUrl = selectionFromParams(searchParams);
    if (fromUrl.kind !== 'unset') {
      prevProjectsRef.current = parseProjectsParam(searchParams);
      setSelection(fromUrl);
      setHydrated(true);
      return;
    }

    // Fall back to localStorage if no URL param
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY);
    } catch {
      // Storage can be unavailable; treat as a first visit
    }
    const fromStorage = parseStoredSelection(stored);
    prevProjectsRef.current = fromStorage.kind === 'projects' ? fromStorage.ids : [];
    setSelection(fromStorage);
    setHydrated(true);
  }, [searchParams]);

  // A first visit opens on the most recently active project. Every project at
  // once stays available, but as a choice the viewer makes, not a default.
  const needsDefault = hydrated && contextEnabled && selection.kind === 'unset';
  const { data: projectsData, isError: projectsFailed } = useProjects({ enabled: needsDefault });
  useEffect(() => {
    if (!needsDefault) return;
    if (projectsFailed) {
      setSelection(ALL);
      return;
    }
    if (!projectsData) return;
    const recent = mostRecentProjectId(projectsData.entities ?? []);
    if (recent) {
      prevProjectsRef.current = [recent];
      setSelection({ kind: 'projects', ids: [recent] });
    } else {
      // Nothing to scope to yet
      setSelection(ALL);
    }
  }, [needsDefault, projectsData, projectsFailed]);

  // Sync to localStorage when selection changes (after hydration)
  useEffect(() => {
    if (!isHydrated.current) return;
    const serialized = serializeSelection(selection);
    if (serialized === null) return;
    try {
      localStorage.setItem(STORAGE_KEY, serialized);
    } catch {
      // Storage can be unavailable
    }
  }, [selection]);

  // Sync URL when USER changes selection (not from URL navigation)
  const userChangedSelection = useRef(false);
  useEffect(() => {
    if (!isHydrated.current) return;
    if (!userChangedSelection.current) return;
    userChangedSelection.current = false;

    const params = new URLSearchParams(searchParams);

    if (selection.kind === 'projects') {
      params.set('projects', selection.ids.join(','));
    } else if (selection.kind === 'all') {
      params.set('projects', ALL_PROJECTS_PARAM);
    } else {
      params.delete('projects');
    }

    const newUrl = params.toString() ? `${pathname}?${params}` : pathname;
    router.replace(newUrl, { scroll: false });
  }, [selection, pathname, router, searchParams]);

  // Sync from URL on external navigation (e.g., back/forward, link click)
  useEffect(() => {
    if (!isHydrated.current) return;

    const fromUrl = selectionFromParams(searchParams);
    if (fromUrl.kind === 'unset') return;
    const projects = parseProjectsParam(searchParams);

    // Only sync if URL differs from what we have
    if (JSON.stringify(projects) !== JSON.stringify(prevProjectsRef.current)) {
      prevProjectsRef.current = projects;
      setSelection(fromUrl);
    }
  }, [searchParams]);

  // Wrapped setters that mark user-initiated changes
  const setProjects = useCallback((projectIds: string[]) => {
    userChangedSelection.current = true;
    prevProjectsRef.current = projectIds;
    setSelection(projectsSelection(projectIds));
  }, []);

  const selectProject = useCallback((projectId: string) => {
    userChangedSelection.current = true;
    prevProjectsRef.current = [projectId];
    setSelection({ kind: 'projects', ids: [projectId] });
  }, []);

  const toggleProject = useCallback((projectId: string) => {
    userChangedSelection.current = true;
    setSelection(prev => {
      const current = prev.kind === 'projects' ? prev.ids : [];
      const next = current.includes(projectId)
        ? current.filter(id => id !== projectId)
        : [...current, projectId];
      prevProjectsRef.current = next;
      return projectsSelection(next);
    });
  }, []);

  // "All projects" is a choice the viewer makes, and it is remembered as one.
  const clearProjects = useCallback(() => {
    userChangedSelection.current = true;
    prevProjectsRef.current = [];
    setSelection(ALL);
  }, []);

  const value = useMemo(
    () => ({
      selectedProjects,
      isAll,
      toggleProject,
      setProjects,
      selectProject,
      clearProjects,
      contextEnabled,
      scopeReady,
    }),
    [
      selectedProjects,
      isAll,
      toggleProject,
      setProjects,
      selectProject,
      clearProjects,
      contextEnabled,
      scopeReady,
    ]
  );

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProjectContext(): ProjectContextValue {
  const context = useContext(ProjectContext);
  if (!context) {
    throw new Error('useProjectContext must be used within ProjectContextProvider');
  }
  return context;
}

/**
 * Hook that returns project filter params for API calls.
 * Returns undefined when "all projects", when multiple projects are selected,
 * or on cross-project pages.
 */
export function useProjectFilter(): string | undefined {
  const { selectedProjects, isAll, contextEnabled } = useProjectContext();

  if (!contextEnabled || isAll) {
    return undefined;
  }

  return selectedProjects.length === 1 ? selectedProjects[0] : undefined;
}

/**
 * Hook that returns all selected project IDs for pages that support
 * multi-project filtering.
 */
export function useProjectFilters(): string[] | undefined {
  const { selectedProjects, isAll, contextEnabled } = useProjectContext();

  if (!contextEnabled || isAll || selectedProjects.length === 0) {
    return undefined;
  }

  return selectedProjects;
}
