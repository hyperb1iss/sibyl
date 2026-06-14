import { describe, expect, it, vi } from 'vitest';
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

function RealtimeHarness() {
  useRealtimeUpdates(true);
  return null;
}

describe('useRealtimeUpdates', () => {
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

    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: queryKeys.rawCaptures.all });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: queryKeys.rawCaptures.detail('raw-a'),
    });
    expect(invalidateQueries).toHaveBeenCalledWith({
      queryKey: queryKeys.rawCaptures.detail('raw-b'),
    });
  });
});
