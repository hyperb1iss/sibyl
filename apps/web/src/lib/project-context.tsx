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

  // Initialize from URL or localStorage
  const [selectedProjects, setSelectedProjectsState] = useState<string[]>(() => {
    // First check URL params
    const urlProjects = searchParams.get('projects');
    if (urlProjects) {
      return urlProjects.split(',').filter(Boolean);
    }

    // Then check localStorage
    if (typeof window !== 'undefined') {
      try {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (stored) {
          const parsed = JSON.parse(stored);
          if (Array.isArray(parsed)) {
            return parsed;
          }
        }
      } catch {
        // Ignore parse errors
      }
    }

    return [];
  });

  const isAll = selectedProjects.length === 0;

  // Sync to localStorage when selection changes
  useEffect(() => {
    if (typeof window !== 'undefined') {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(selectedProjects));
    }
  }, [selectedProjects]);

  // Sync URL when selection changes (separate effect to avoid loop)
  const prevProjectsRef = useRef<string[]>(selectedProjects);
  useEffect(() => {
    // Only update URL if state actually changed (not from URL sync)
    if (JSON.stringify(prevProjectsRef.current) === JSON.stringify(selectedProjects)) {
      return;
    }
    prevProjectsRef.current = selectedProjects;

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

  const setProjects = useCallback((projectIds: string[]) => {
    setSelectedProjectsState(projectIds);
  }, []);

  const selectProject = useCallback((projectId: string) => {
    setSelectedProjectsState([projectId]);
  }, []);

  const toggleProject = useCallback((projectId: string) => {
    setSelectedProjectsState(prev => {
      return prev.includes(projectId) ? prev.filter(id => id !== projectId) : [...prev, projectId];
    });
  }, []);

  const clearProjects = useCallback(() => {
    setSelectedProjectsState([]);
  }, []);

  // Sync from URL on navigation (when URL changes externally)
  useEffect(() => {
    const urlProjects = searchParams.get('projects');
    const projects = urlProjects ? urlProjects.split(',').filter(Boolean) : [];

    // Only sync if URL projects differ from our tracked state (use ref to avoid deps)
    if (JSON.stringify(projects) !== JSON.stringify(prevProjectsRef.current)) {
      prevProjectsRef.current = projects;
      setSelectedProjectsState(projects);
    }
  }, [searchParams]);

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
