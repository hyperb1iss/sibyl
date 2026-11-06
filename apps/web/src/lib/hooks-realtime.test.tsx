import { useQuery } from '@tanstack/react-query';
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
import { createSettledInvalidationScheduler, NET_REFETCH_DELAY_MS } from './hooks/net';
import { INVALIDATION_DEBOUNCE_MS } from './hooks/shared';

const SLOW_FEED_KEY = ['net', 'feed', '@ada', null] as const;
const slowReads = { started: 0, finished: 0, aborted: 0 };

/** A feed whose every read takes four seconds and notes whether it was cut off. */
function SlowFeed() {
  const { data } = useQuery({
    queryKey: SLOW_FEED_KEY,
    queryFn: ({ signal }) =>
      new Promise<number>((resolve, reject) => {
        slowReads.started += 1;
        const timer = setTimeout(() => {
          slowReads.finished += 1;
          resolve(slowReads.finished);
        }, 4000);
        signal.addEventListener('abort', () => {
          clearTimeout(timer);
          slowReads.aborted += 1;
          reject(new Error('aborted'));
        });
      }),
  });
  return <p>{data ?? 'loading'}</p>;
}

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

  describe('net_message_posted', () => {
    function netKeys(spy: { mock: { calls: unknown[][] } }): string[] {
      return spy.mock.calls.map(([filters]) =>
        JSON.stringify((filters as { queryKey?: unknown } | undefined)?.queryKey)
      );
    }

    async function mountAndPost(payload: { channel: string; thread: string }) {
      const queryClient = createTestQueryClient();
      const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries');
      render(<RealtimeHarness />, { queryClient });
      await waitFor(() => {
        expect(websocket.handlers.has('net_message_posted')).toBe(true);
      });
      const post = () =>
        websocket.handlers.get('net_message_posted')?.({
          organization_id: 'org-1',
          message_id: 'msg-1',
          ...payload,
        });
      return { invalidateQueries, post };
    }

    it('waits out the settle window, then refetches what the post touched', async () => {
      const { invalidateQueries, post } = await mountAndPost({
        channel: '#reviews',
        thread: 'gh:acme/app#42',
      });

      post();
      // The feed would not return the message yet; a refetch now would miss it.
      await vi.advanceTimersByTimeAsync(NET_REFETCH_DELAY_MS - 100);
      expect(invalidateQueries).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(100);
      const keys = netKeys(invalidateQueries);
      for (const key of [
        queryKeys.net.channels,
        queryKeys.net.feed(null, null),
        queryKeys.net.feed('#reviews', null),
        queryKeys.net.feed('#reviews', 'gh:acme/app#42'),
        queryKeys.net.threads('#reviews'),
        queryKeys.net.presence,
      ]) {
        expect(keys).toContain(JSON.stringify(key));
      }
    });

    it('leaves thread feeds alone for a threadless post', async () => {
      const { invalidateQueries, post } = await mountAndPost({ channel: '@ada', thread: '' });

      post();
      await vi.advanceTimersByTimeAsync(NET_REFETCH_DELAY_MS);

      const keys = netKeys(invalidateQueries);
      expect(keys).toContain(JSON.stringify(queryKeys.net.feed('@ada', null)));
      expect(keys.filter(key => key.startsWith('["net","feed","@ada",'))).toHaveLength(1);
    });

    it('keeps refreshing while two agents post back and forth every second', async () => {
      const { invalidateQueries, post } = await mountAndPost({
        channel: '@ada',
        thread: 'loop',
      });
      const streamKey = JSON.stringify(queryKeys.net.feed('@ada', 'loop'));

      for (let second = 0; second < 10; second++) {
        post();
        await vi.advanceTimersByTimeAsync(1000);
      }
      // A trailing debounce would still be waiting for the loop to stop.
      const duringLoop = netKeys(invalidateQueries).filter(key => key === streamKey).length;
      expect(duringLoop).toBeGreaterThanOrEqual(3);

      // The last post, a second ago, is fetched once it settles, and then it stops.
      await vi.advanceTimersByTimeAsync(NET_REFETCH_DELAY_MS - 1000 - 100);
      const beforeSettled = netKeys(invalidateQueries).filter(key => key === streamKey).length;
      await vi.advanceTimersByTimeAsync(200);
      const settled = netKeys(invalidateQueries).filter(key => key === streamKey).length;
      expect(settled).toBe(beforeSettled + 1);
      await vi.advanceTimersByTimeAsync(NET_REFETCH_DELAY_MS * 3);
      expect(netKeys(invalidateQueries).filter(key => key === streamKey)).toHaveLength(settled);
    });
  });

  it('lets a slow refetch finish while posts keep arriving', async () => {
    const queryClient = createTestQueryClient();
    const reads = slowReads;
    reads.started = 0;
    reads.finished = 0;
    reads.aborted = 0;
    const key = SLOW_FEED_KEY;

    render(<SlowFeed />, { queryClient });
    await vi.advanceTimersByTimeAsync(4000);
    expect(reads.finished).toBe(1);

    // A post every second for twenty seconds, each refetch taking four.
    const scheduler = createSettledInvalidationScheduler(queryClient);
    for (let second = 0; second < 20; second++) {
      scheduler.schedule(key);
      await vi.advanceTimersByTimeAsync(1000);
    }
    await vi.advanceTimersByTimeAsync(NET_REFETCH_DELAY_MS + 4000);
    scheduler.cancel();

    expect(reads.aborted).toBe(0);
    expect(reads.finished).toBeGreaterThanOrEqual(4);
    expect(reads.finished).toBe(reads.started);
  });
});
