import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { TaskListResponse, TaskSummary } from '../api/work-items';

const api = vi.hoisted(() => ({ list: vi.fn() }));

vi.mock('../api/work-items', async importOriginal => {
  const original = await importOriginal<typeof import('../api/work-items')>();
  return { ...original, tasksApi: { ...original.tasksApi, list: api.list } };
});

import { LEGACY_TASK_PAGE_SIZE, TASK_PAGE_SIZE } from '../api/work-items';
import { fetchAllTasks } from './work-items';

function page(ids: string[], hasMore: boolean, actualTotal: number): TaskListResponse {
  return {
    mode: 'list',
    filters: {},
    total: ids.length,
    has_more: hasMore,
    actual_total: actualTotal,
    entities: ids.map(
      id => ({ id, type: 'task', name: id, description: '', metadata: {} }) as TaskSummary
    ),
  };
}

describe('fetchAllTasks', () => {
  beforeEach(() => {
    api.list.mockReset();
  });

  it('pages through the explore list until the server says there is no more', async () => {
    api.list
      .mockResolvedValueOnce(page(['a', 'b'], true, 3))
      .mockResolvedValueOnce(page(['c'], false, 3));

    const result = await fetchAllTasks({ project_ids: ['p1'] });

    expect(result.entities.map(task => task.id)).toEqual(['a', 'b', 'c']);
    expect(result.total).toBe(3);
    expect(result.actual_total).toBe(3);
    expect(result.has_more).toBe(false);
    expect(api.list).toHaveBeenNthCalledWith(
      1,
      { project_ids: ['p1'] },
      { limit: TASK_PAGE_SIZE, offset: 0 }
    );
    expect(api.list).toHaveBeenNthCalledWith(
      2,
      { project_ids: ['p1'] },
      { limit: TASK_PAGE_SIZE, offset: 2 }
    );
  });

  it('stops after a single page when the server has nothing more', async () => {
    api.list.mockResolvedValueOnce(page(['a'], false, 1));

    const result = await fetchAllTasks(undefined);

    expect(result.entities).toHaveLength(1);
    expect(api.list).toHaveBeenCalledTimes(1);
  });

  it('never loops forever on a server that always claims more', async () => {
    api.list.mockResolvedValue(page(['x'], true, 999));

    const result = await fetchAllTasks(undefined);

    expect(api.list).toHaveBeenCalledTimes(25);
    expect(result.entities).toHaveLength(25);
    expect(result.has_more).toBe(true);
  });

  it('drops to the legacy page size when the API rejects the bigger page', async () => {
    api.list
      .mockRejectedValueOnce(
        new Error(
          '{"error":"validation_error","details":{"field":"body.limit","expected":"less_than_equal"}}'
        )
      )
      .mockResolvedValueOnce(page(['a', 'b'], true, 3))
      .mockResolvedValueOnce(page(['c'], false, 3));

    const result = await fetchAllTasks(undefined);

    expect(result.entities.map(task => task.id)).toEqual(['a', 'b', 'c']);
    expect(api.list).toHaveBeenNthCalledWith(1, undefined, { limit: TASK_PAGE_SIZE, offset: 0 });
    expect(api.list).toHaveBeenNthCalledWith(2, undefined, {
      limit: LEGACY_TASK_PAGE_SIZE,
      offset: 0,
    });
    expect(api.list).toHaveBeenNthCalledWith(3, undefined, {
      limit: LEGACY_TASK_PAGE_SIZE,
      offset: 2,
    });
  });

  it('rethrows every other error', async () => {
    api.list.mockRejectedValueOnce(new Error('API error: 500'));

    await expect(fetchAllTasks(undefined)).rejects.toThrow('API error: 500');
    expect(api.list).toHaveBeenCalledTimes(1);
  });

  it('advances past a page the server filtered empty', async () => {
    api.list
      .mockResolvedValueOnce(page([], true, 2))
      .mockResolvedValueOnce(page(['a', 'b'], false, 2));

    const result = await fetchAllTasks(undefined);

    expect(result.entities.map(task => task.id)).toEqual(['a', 'b']);
    expect(result.has_more).toBe(false);
    expect(api.list).toHaveBeenNthCalledWith(2, undefined, {
      limit: TASK_PAGE_SIZE,
      offset: TASK_PAGE_SIZE,
    });
  });
});
