'use client';

import { Folder } from '@/components/ui/icons';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

export const ALL_PROJECTS_VALUE = 'all';
const SEVERAL_PROJECTS_VALUE = '__several__';

export interface ProjectOption {
  id: string;
  name: string;
}

interface ProjectScopeSelectProps {
  projects: ProjectOption[];
  /** Names for projects the option list leaves out, such as archived ones. */
  projectNames?: Record<string, string>;
  /** The global project selection; empty means every project. */
  selectedIds: string[];
  onSelectAll: () => void;
  onSelectProject: (projectId: string) => void;
}

/**
 * Picks the project scope through the app-wide project selection, so the
 * header selector, this control, and the `projects` URL parameter agree.
 */
export function ProjectScopeSelect({
  projects,
  projectNames = {},
  selectedIds,
  onSelectAll,
  onSelectProject,
}: ProjectScopeSelectProps) {
  const value =
    selectedIds.length === 0
      ? ALL_PROJECTS_VALUE
      : selectedIds.length === 1
        ? selectedIds[0]
        : SEVERAL_PROJECTS_VALUE;
  const label =
    selectedIds.length === 0
      ? 'All projects'
      : selectedIds.length === 1
        ? (projects.find(project => project.id === selectedIds[0])?.name ??
          projectNames[selectedIds[0]] ??
          'Selected project')
        : `${selectedIds.length} projects`;

  // A selected project the list does not carry (archived, or past the first
  // page) still needs an item, or the trigger would read as empty.
  const options =
    selectedIds.length === 1 && !projects.some(project => project.id === selectedIds[0])
      ? [...projects, { id: selectedIds[0], name: label }]
      : projects;

  return (
    <Select
      value={value}
      onValueChange={next => {
        if (next === ALL_PROJECTS_VALUE) onSelectAll();
        else if (next !== SEVERAL_PROJECTS_VALUE) onSelectProject(next);
      }}
    >
      <SelectTrigger
        aria-label="Project scope"
        className="h-[42px] min-w-0 sm:w-[220px] focus-visible:ring-offset-sc-bg-elevated"
      >
        <span className="flex min-w-0 items-center gap-2">
          <Folder width={14} height={14} aria-hidden="true" className="shrink-0 text-sc-fg-muted" />
          <SelectValue>
            <span className="truncate">{label}</span>
          </SelectValue>
        </span>
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL_PROJECTS_VALUE}>All projects</SelectItem>
        {selectedIds.length > 1 && (
          <SelectItem value={SEVERAL_PROJECTS_VALUE} disabled>
            {label}
          </SelectItem>
        )}
        {options.map(project => (
          <SelectItem key={project.id} value={project.id}>
            {project.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
