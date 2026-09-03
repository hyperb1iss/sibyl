import { fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { TaskSummary } from '@/lib/api/work-items';
import { render, screen, within } from '@/test/utils';
import { KanbanBoard } from './kanban-board';

vi.mock('@/lib/project-context', () => ({
  useProjectFilter: () => undefined,
  useProjectContext: () => ({ selectProject: vi.fn() }),
}));

function task(
  id: string,
  status: string,
  extra: Partial<TaskSummary['metadata']> = {}
): TaskSummary {
  return {
    id,
    type: 'task',
    name: `Task ${id}`,
    description: '',
    metadata: { status, priority: 'medium', ...extra },
  } as TaskSummary;
}

function column(label: string) {
  return screen.getByRole('region', { name: `${label} column` });
}

describe('KanbanBoard', () => {
  it('folds empty columns into rails and keeps populated columns fluid', () => {
    render(<KanbanBoard tasks={[task('a', 'todo'), task('b', 'doing')]} />);

    expect(column('Todo')).not.toHaveAttribute('data-collapsed');
    expect(column('Doing')).not.toHaveAttribute('data-collapsed');
    for (const label of ['Backlog', 'Blocked', 'Review', 'Done']) {
      const rail = column(label);
      expect(rail).toHaveAttribute('data-collapsed');
      expect(within(rail).getByText(label)).toBeInTheDocument();
      expect(within(rail).queryByText('No tasks')).not.toBeInTheDocument();
    }
    expect(within(column('Todo')).getByText('Task a')).toBeInTheDocument();
  });

  it('unfolds every column while a drag is in flight', () => {
    render(<KanbanBoard tasks={[task('a', 'todo')]} />);

    const dataTransfer = { setData: vi.fn(), effectAllowed: 'none' };
    fireEvent.dragStart(screen.getByText('Task a'), { dataTransfer });
    expect(column('Blocked')).not.toHaveAttribute('data-collapsed');
    expect(within(column('Blocked')).getByText('Drag tasks here')).toBeInTheDocument();

    fireEvent.dragEnd(screen.getByText('Task a'), { dataTransfer });
    expect(column('Blocked')).toHaveAttribute('data-collapsed');
  });

  it('windows long columns behind a show-more row without hiding the count', () => {
    const tasks = Array.from({ length: 55 }, (_, i) => task(`t${i}`, 'todo'));
    render(<KanbanBoard tasks={tasks} />);

    const todo = column('Todo');
    expect(within(todo).getByText('55')).toBeInTheDocument();
    expect(within(todo).getAllByText(/^Task t/)).toHaveLength(40);

    fireEvent.click(within(todo).getByRole('button', { name: /Show 15 more/ }));
    expect(within(todo).getAllByText(/^Task t/)).toHaveLength(55);
    expect(within(todo).queryByRole('button', { name: /more/ })).not.toBeInTheDocument();
  });
});
