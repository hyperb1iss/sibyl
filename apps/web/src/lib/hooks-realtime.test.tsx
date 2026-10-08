import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createTestQueryClient, render, waitFor } from '@/test/utils';

const websocket = vi.hoisted(() => {
  type Handler = (data: unknown) => void;
  const handlers = new Map<string, Handler>();
  return {
    handlers,
    wsClient: {
      status: 'connected',
      connect: vi.fn(),
      disconnect: vi.fn(),
      on: vi.fn((event: string, handler: Handler) => {
        handlers.set(event, handler);
        return vi.fn();
      }),
    },
  };
});

vi.mock('./websocket', () => ({
  wsClient: websocket.wsClient,
}));

import { queryKeys, useRealtimeUpdates } from './hooks';
import { INVALIDATION_DEBOUNCE_MS } from './hooks/shared';

function RealtimeHarness() {
  useRealtimeUpdates(true);
  return null;
}

describe('useRealtimeUpdates', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('refreshes raw capture queries when raw captures change', async () => {
    const queryClient = createTestQueryClient();
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries');

    render(<RealtimeHarness />, { queryClient });

    await waitFor(() => {
      expect(websocket.handlers.has('raw_capture_changed')).toBe(true);
    });

    websocket.handlers.get('raw_capture_changed')?.({
      organization_id: 'org-1',
      raw_memory_ids: ['raw-a', 'raw-b'],
      promotion_job_id: 'raw_promotion:queued',
      rows_seen: 2,
      previous_versionstamp: 3,
      next_versionstamp: 9,
    });
    await vi.advanceTimersByTimeAsync(INVALIDATION_DEBOUNCE_MS);

    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: queryKeys.rawCaptures.all });
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: queryKeys.activity.all });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: queryKeys.rawCaptures.detail('raw-a'),
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: queryKeys.rawCaptures.detail('raw-b'),
    });
  });

  it.each([
    ['note_created', { task_id: 'task_1', note_id: 'note_1' }],
    ['permission_changed', { user_id: 'user-1', change_type: 'org_member_added' }],
  ])('refreshes team activity on %s', async (event, payload) => {
    const queryClient = createTestQueryClient();
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries');

    render(<RealtimeHarness />, { queryClient });

    await waitFor(() => {
      expect(websocket.handlers.has(event)).toBe(true);
    });

    websocket.handlers.get(event)?.(payload);
    await vi.advanceTimersByTimeAsync(INVALIDATION_DEBOUNCE_MS);

    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: queryKeys.activity.all });
  });

  it('coalesces a burst of task events into one refetch per query key', async () => {
    const queryClient = createTestQueryClient();
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries');

    render(<RealtimeHarness />, { queryClient });

    await waitFor(() => {
      expect(websocket.handlers.has('entity_updated')).toBe(true);
    });

    for (let index = 0; index < 20; index++) {
      websocket.handlers.get('entity_updated')?.({ id: 'task_1', entity_type: 'task' });
      await vi.advanceTimersByTimeAsync(5);
    }
    // Nothing has fired while the burst is still arriving.
    expect(invalidateQueries).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(INVALIDATION_DEBOUNCE_MS);

    const keys = invalidateQueries.mock.calls.map(([filters]) => JSON.stringify(filters?.queryKey));
    expect(keys.filter(key => key === JSON.stringify(queryKeys.tasks.all))).toHaveLength(1);
    expect(keys.filter(key => key === JSON.stringify(['metrics']))).toHaveLength(1);
    expect(keys.filter(key => key === JSON.stringify(queryKeys.activity.all))).toHaveLength(1);
    expect(
      keys.filter(key => key === JSON.stringify(queryKeys.tasks.detail('task_1')))
    ).toHaveLength(1);
    expect(
      keys.filter(key => key === JSON.stringify(queryKeys.explore.related('task_1')))
    ).toHaveLength(1);
  });

  it('drops pending invalidations when the subscription unmounts', async () => {
    const queryClient = createTestQueryClient();
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries');

    const { unmount } = render(<RealtimeHarness />, { queryClient });
    await waitFor(() => {
      expect(websocket.handlers.has('entity_created')).toBe(true);
    });

    websocket.handlers.get('entity_created')?.({ id: 'pattern_1', entity_type: 'pattern' });
    unmount();
    await vi.advanceTimersByTimeAsync(INVALIDATION_DEBOUNCE_MS * 2);

    expect(invalidateQueries).not.toHaveBeenCalled();
  });
});
