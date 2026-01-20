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

const STORAGE_KEY = 'sibyl-project-context';

// Pages that should always show all projects (no filtering)
const CROSS_PROJECT_PATHS = ['/projects', '/sources', '/settings'];

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

  // Initialize with empty array - proper value set in effect after hydration
  const [selectedProjects, setSelectedProjectsState] = useState<string[]>([]);

  const isAll = selectedProjects.length === 0;

  // Initial hydration: sync from URL (primary) or localStorage (fallback)
  // This runs once after mount to ensure searchParams is available
  useEffect(() => {
    if (isHydrated.current) return;
    isHydrated.current = true;

    // URL is source of truth
    const urlProjects = searchParams.get('projects');
    if (urlProjects) {
      const projects = urlProjects.split(',').filter(Boolean);
      prevProjectsRef.current = projects;
      setSelectedProjectsState(projects);
      return;
    }

    // Fall back to localStorage if no URL param
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed) && parsed.length > 0) {
          prevProjectsRef.current = parsed;
          setSelectedProjectsState(parsed);
          return;
        }
      }
    } catch {
      // Ignore parse errors
    }

    // No URL and no localStorage - stay with empty (all projects)
    prevProjectsRef.current = [];
  }, [searchParams]);

  // Sync to localStorage when selection changes (after hydration)
  useEffect(() => {
    if (!isHydrated.current) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(selectedProjects));
  }, [selectedProjects]);

  // Sync URL when USER changes selection (not from URL navigation)
  const userChangedSelection = useRef(false);
  useEffect(() => {
    if (!isHydrated.current) return;
    if (!userChangedSelection.current) return;
    userChangedSelection.current = false;

    const params = new URLSearchParams(searchParams);
    params.delete('project'); // Remove legacy single 'project' param

    if (selectedProjects.length > 0) {
      params.set('projects', selectedProjects.join(','));
    } else {
      params.delete('projects');
    }

    const newUrl = params.toString() ? `${pathname}?${params}` : pathname;
    router.replace(newUrl, { scroll: false });
  }, [selectedProjects, pathname, router, searchParams]);

  // Sync from URL on external navigation (e.g., back/forward, link click)
  useEffect(() => {
    if (!isHydrated.current) return;

    const urlProjects = searchParams.get('projects');
    const projects = urlProjects ? urlProjects.split(',').filter(Boolean) : [];

    // Only sync if URL differs from what we have
    if (JSON.stringify(projects) !== JSON.stringify(prevProjectsRef.current)) {
      prevProjectsRef.current = projects;
      setSelectedProjectsState(projects);
    }
  }, [searchParams]);

  // Wrapped setters that mark user-initiated changes
  const setProjects = useCallback((projectIds: string[]) => {
    userChangedSelection.current = true;
    prevProjectsRef.current = projectIds;
    setSelectedProjectsState(projectIds);
  }, []);

  const selectProject = useCallback((projectId: string) => {
    userChangedSelection.current = true;
    prevProjectsRef.current = [projectId];
    setSelectedProjectsState([projectId]);
  }, []);

  const toggleProject = useCallback((projectId: string) => {
    userChangedSelection.current = true;
    setSelectedProjectsState(prev => {
      const next = prev.includes(projectId)
        ? prev.filter(id => id !== projectId)
        : [...prev, projectId];
      prevProjectsRef.current = next;
      return next;
    });
  }, []);

  const clearProjects = useCallback(() => {
    userChangedSelection.current = true;
    prevProjectsRef.current = [];
    setSelectedProjectsState([]);
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
    }),
    [
      selectedProjects,
      isAll,
      toggleProject,
      setProjects,
      selectProject,
      clearProjects,
      contextEnabled,
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
 * Returns undefined when "all projects" or on cross-project pages.
 */
export function useProjectFilter(): string | undefined {
  const { selectedProjects, isAll, contextEnabled } = useProjectContext();

  if (!contextEnabled || isAll) {
    return undefined;
  }

  // For now, API supports single project filter
  // When multi-project is needed, update API to accept array
  return selectedProjects[0];
}
