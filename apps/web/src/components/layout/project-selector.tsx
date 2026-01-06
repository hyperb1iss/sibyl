'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, Folder, X } from '@/components/ui/icons';
import { useProjects } from '@/lib/hooks';
import { useProjectContext } from '@/lib/project-context';

/**
 * Global project context selector.
 * Shown in header, controls which projects are visible across the app.
 */
export function ProjectSelector() {
  const { selectedProjects, isAll, toggleProject, clearProjects, selectProject, contextEnabled } =
    useProjectContext();
  const { data: projectsData } = useProjects();

  // Sort projects by recency (prefer last_activity_at, fall back to updated_at)
  const projects = [...(projectsData?.entities ?? [])].sort((a, b) => {
    const aActivity = a.metadata?.last_activity_at || a.metadata?.updated_at;
    const bActivity = b.metadata?.last_activity_at || b.metadata?.updated_at;
    const aTime = aActivity ? new Date(aActivity as string).getTime() : 0;
    const bTime = bActivity ? new Date(bActivity as string).getTime() : 0;
    return bTime - aTime;
  });

  const [isOpen, setIsOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => document.removeEventListener('mousedown', handleClickOutside);
    }
  }, [isOpen]);

  // Close on escape
  useEffect(() => {
    function handleEscape(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setIsOpen(false);
      }
    }
    if (isOpen) {
      document.addEventListener('keydown', handleEscape);
      return () => document.removeEventListener('keydown', handleEscape);
    }
  }, [isOpen]);

  // Get display text and count separately to prevent layout shift
  const displayInfo = useMemo(() => {
    if (isAll || selectedProjects.length === 0) {
      return { name: 'All Projects', count: 0 };
    }
    const firstProject = projects.find(p => p.id === selectedProjects[0]);
    const firstName = firstProject?.name ?? 'Project';
    return { name: firstName, count: selectedProjects.length - 1 };
  }, [isAll, selectedProjects, projects]);

  // Handle single click (quick switch to single project)
  const handleQuickSelect = useCallback(
    (projectId: string) => {
      selectProject(projectId);
      setIsOpen(false);
    },
    [selectProject]
  );

  // Handle checkbox toggle (multi-select)
  const handleToggle = useCallback(
    (e: React.MouseEvent, projectId: string) => {
      e.stopPropagation();
      toggleProject(projectId);
    },
    [toggleProject]
  );

  // Don't show on cross-project pages
  if (!contextEnabled) {
    return null;
  }

  return (
    <div ref={dropdownRef} className="relative">
      {/* Trigger Button */}
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className={`
          flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium
          transition-all duration-200 w-[180px]
          ${
            isOpen
              ? 'bg-sc-purple/20 text-sc-purple border-sc-purple/40'
              : isAll
                ? 'bg-sc-bg-elevated text-sc-fg-muted border-sc-fg-subtle/20 hover:border-sc-purple/40 hover:text-sc-fg-primary'
                : 'bg-sc-purple/10 text-sc-purple border-sc-purple/30 hover:bg-sc-purple/20'
          }
          border
        `}
      >
        <Folder width={14} height={14} className="shrink-0" />
        <span className="flex-1 truncate text-left">{displayInfo.name}</span>
        {displayInfo.count > 0 && (
          <span className="shrink-0 text-xs opacity-70">+{displayInfo.count}</span>
        )}
        <ChevronDown
          width={14}
          height={14}
          className={`shrink-0 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Dropdown */}
      {isOpen && (
        <div className="absolute top-full left-0 mt-1 w-64 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl shadow-lg overflow-hidden z-50 animate-fade-in">
          {/* All Projects Option */}
          <button
            type="button"
            onClick={() => {
              clearProjects();
              setIsOpen(false);
            }}
            className={`
              w-full flex items-center gap-3 px-4 py-3 text-left text-sm
              transition-colors
              ${isAll ? 'bg-sc-purple/10 text-sc-purple' : 'text-sc-fg-muted hover:bg-sc-bg-elevated hover:text-sc-fg-primary'}
            `}
          >
            <div
              className={`
                w-4 h-4 rounded-full border-2 flex items-center justify-center
                ${isAll ? 'border-sc-purple bg-sc-purple' : 'border-sc-fg-subtle/40'}
              `}
            >
              {isAll && <Check width={10} height={10} className="text-white" />}
            </div>
            <span className="font-medium">All Projects</span>
          </button>

          {/* Divider */}
          <div className="border-t border-sc-fg-subtle/10" />

          {/* Project List */}
          <div className="max-h-64 overflow-y-auto">
            {projects.length === 0 ? (
              <div className="px-4 py-6 text-center text-sc-fg-subtle text-sm">No projects yet</div>
            ) : (
              projects.map(project => {
                const isSelected = selectedProjects.includes(project.id);
                const taskCount = (project.metadata?.task_count as number) ?? 0;

                return (
                  <div
                    key={project.id}
                    className={`
                      flex items-center gap-3 px-4 py-2.5 text-sm cursor-pointer
                      transition-colors
                      ${isSelected ? 'bg-sc-purple/5' : 'hover:bg-sc-bg-elevated'}
                    `}
                    onClick={() => handleQuickSelect(project.id)}
                    onKeyDown={e => e.key === 'Enter' && handleQuickSelect(project.id)}
                    role="button"
                    tabIndex={0}
                  >
                    {/* Checkbox for multi-select */}
                    <button
                      type="button"
                      onClick={e => handleToggle(e, project.id)}
                      className={`
                        w-4 h-4 rounded border flex items-center justify-center
                        transition-colors
                        ${isSelected ? 'bg-sc-purple border-sc-purple' : 'border-sc-fg-subtle/40 hover:border-sc-purple/60'}
                      `}
                    >
                      {isSelected && <Check width={10} height={10} className="text-white" />}
                    </button>

                    {/* Project Info */}
                    <div className="flex-1 min-w-0">
                      <div
                        className={`font-medium truncate ${isSelected ? 'text-sc-purple' : 'text-sc-fg-primary'}`}
                      >
                        {project.name}
                      </div>
                    </div>

                    {/* Task Count */}
                    {taskCount > 0 && (
                      <span className="text-xs text-sc-fg-subtle">{taskCount} tasks</span>
                    )}
                  </div>
                );
              })
            )}
          </div>

          {/* Footer - Clear Selection (when multi-selected) */}
          {selectedProjects.length > 1 && (
            <>
              <div className="border-t border-sc-fg-subtle/10" />
              <button
                type="button"
                onClick={() => {
                  clearProjects();
                  setIsOpen(false);
                }}
                className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-xs text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-elevated transition-colors"
              >
                <X width={12} height={12} />
                Clear selection
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
