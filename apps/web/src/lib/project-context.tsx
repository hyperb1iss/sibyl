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
type ProjectSelection =
  | { kind: 'unset' }
  | { kind: 'all'; transient?: boolean }
  | { kind: 'projects'; ids: string[] };

const UNSET: ProjectSelection = { kind: 'unset' };
const ALL: ProjectSelection = { kind: 'all' };
/**
 * Every project because nothing better was available (the project list
 * failed to load, or the org has no projects yet). It is never saved, so the
 * next visit with a healthy backend still opens on a project.
 */
const FALLBACK_ALL: ProjectSelection = { kind: 'all', transient: true };

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
  if (selection.kind === 'all') {
    return selection.transient ? null : JSON.stringify({ mode: 'all' });
  }
  return JSON.stringify({ projects: selection.ids });
}

/** A selection's content, for telling whether the one on screen is the one queued for the URL. */
function selectionKey(selection: ProjectSelection): string {
  return selection.kind === 'projects' ? `projects:${selection.ids.join(',')}` : selection.kind;
}

/** The active project with the latest activity, the one a fresh visit opens on. */
export function mostRecentProjectId(
  projects: Array<{ id: string; metadata?: Record<string, unknown> | null }>
): string | null {
  let best: { id: string; time: number } | null = null;
  for (const project of projects) {
    if (project.metadata?.status === 'archived') continue;
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
  // The selection a change asked to write back to the URL. The URL effect
  // writes only once that selection is the one on screen: a flag would be
  // consumed by an effect run that still sees the previous selection when
  // the change is made from inside another effect.
  const pendingUrlWrite = useRef<string | null>(null);

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
  // A chosen set of projects is checked against the same list, so an id that
  // was deleted, lost its access, or belongs to another org drops out.
  const needsDefault = hydrated && contextEnabled && selection.kind === 'unset';
  const needsValidation = hydrated && contextEnabled && selection.kind === 'projects';
  // Validation reads archived projects too, since links from the Projects
  // page and task views can open one on purpose; the default never picks one.
  const { data: projectsData, isError: projectsFailed } = useProjects({
    includeArchived: needsValidation,
    enabled: needsDefault || needsValidation,
  });
  useEffect(() => {
    if (!needsDefault && !needsValidation) return;
    if (projectsFailed) {
      // Keep a chosen set as it is; only a missing choice falls back.
      if (needsDefault) setSelection(FALLBACK_ALL);
      return;
    }
    if (!projectsData) return;
    const entities = projectsData.entities ?? [];
    let next: ProjectSelection | null = null;
    if (selection.kind === 'projects') {
      // A truncated list cannot prove an id is gone, only that it did not fit
      if (projectsData.has_more) return;
      const known = new Set(entities.map(project => project.id));
      const kept = selection.ids.filter(id => known.has(id));
      if (kept.length === selection.ids.length) return;
      if (kept.length > 0) next = { kind: 'projects', ids: kept };
    }
    if (!next) {
      const recent = mostRecentProjectId(entities);
      // Nothing to scope to yet: every project, but not as a saved choice
      next = recent ? { kind: 'projects', ids: [recent] } : FALLBACK_ALL;
    }
    prevProjectsRef.current = next.kind === 'projects' ? next.ids : [];
    // A corrected choice that came from the URL rewrites the URL too
    if (searchParams.get('projects') !== null) pendingUrlWrite.current = selectionKey(next);
    setSelection(next);
  }, [needsDefault, needsValidation, projectsData, projectsFailed, selection, searchParams]);

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
  useEffect(() => {
    if (!isHydrated.current) return;
    if (pendingUrlWrite.current === null) return;
    if (pendingUrlWrite.current !== selectionKey(selection)) return;
    pendingUrlWrite.current = null;

    const params = new URLSearchParams(searchParams);

    if (selection.kind === 'projects') {
      params.set('projects', selection.ids.join(','));
    } else if (selection.kind === 'all' && !selection.transient) {
      params.set('projects', ALL_PROJECTS_PARAM);
    } else {
      // A fallback is not a choice, so it never lands in a shareable URL
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
    const next = projectsSelection(projectIds);
    pendingUrlWrite.current = selectionKey(next);
    prevProjectsRef.current = projectIds;
    setSelection(next);
  }, []);

  const selectProject = useCallback((projectId: string) => {
    const next: ProjectSelection = { kind: 'projects', ids: [projectId] };
    pendingUrlWrite.current = selectionKey(next);
    prevProjectsRef.current = [projectId];
    setSelection(next);
  }, []);

  const toggleProject = useCallback((projectId: string) => {
    setSelection(prev => {
      const current = prev.kind === 'projects' ? prev.ids : [];
      const ids = current.includes(projectId)
        ? current.filter(id => id !== projectId)
        : [...current, projectId];
      const next = projectsSelection(ids);
      // Content, not identity, so a repeated updater call queues the same write
      pendingUrlWrite.current = selectionKey(next);
      prevProjectsRef.current = ids;
      return next;
    });
  }, []);

  // "All projects" is a choice the viewer makes, and it is remembered as one.
  const clearProjects = useCallback(() => {
    pendingUrlWrite.current = selectionKey(ALL);
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
 * Keep something the viewer just created in view. When they have chosen
 * specific projects and create an item in a project outside that choice,
 * the project joins the selection, so the item does not vanish from the list
 * it was created on. An every-project view (chosen or fallback) is left as it
 * is. Returns true when the selection changed, so the caller can say so.
 */
export function useRevealProject(): (projectId: string | undefined) => boolean {
  const { selectedProjects, isAll, contextEnabled, toggleProject } = useProjectContext();
  return useCallback(
    (projectId: string | undefined) => {
      if (!projectId || !contextEnabled || isAll) return false;
      if (selectedProjects.length === 0 || selectedProjects.includes(projectId)) return false;
      toggleProject(projectId);
      return true;
    },
    [contextEnabled, isAll, selectedProjects, toggleProject]
  );
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
