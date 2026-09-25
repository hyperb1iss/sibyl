import { describe, expect, it, vi } from 'vitest';
import type { Entity } from '@/lib/api/graph';
import { projectFilterTarget, render, screen } from '@/test/utils';
import { TaskSidebar } from './task-sidebar';

const task: Entity = {
  id: 'task-1',
  entity_type: 'task',
  name: 'Ship the fix',
  description: '',
  content: '',
  category: null,
  languages: [],
  tags: [],
  metadata: {},
  source_file: null,
  created_at: null,
  updated_at: null,
};

describe('TaskSidebar', () => {
  it('links to the task list scoped to the task project', () => {
    render(
      <TaskSidebar
        task={task}
        projectId="proj-a"
        assignees={[]}
        estimatedHours={undefined}
        actualHours={undefined}
        branchName={undefined}
        prUrl={undefined}
        projectOptions={[{ value: 'proj-a', label: 'Alpha', icon: null }]}
        isDeleting={false}
        onUpdateField={vi.fn()}
        onDelete={vi.fn()}
      />
    );

    const link = screen.getByRole('link', { name: /View Project Tasks/ });
    expect(projectFilterTarget(link.getAttribute('href'))).toEqual({
      pathname: '/tasks',
      projects: ['proj-a'],
    });
  });
});
