import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  isWebSocketEventType,
  WEBSOCKET_BROADCAST_EVENT_TYPES,
  WEBSOCKET_EVENT_TYPES,
  wsClient,
} from './websocket';

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;

  readyState = MockWebSocket.OPEN;
  send = vi.fn();
  close = vi.fn();
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this);
  }
}

describe('websocket event contract', () => {
  afterEach(() => {
    wsClient.destroy();
    MockWebSocket.instances = [];
    vi.unstubAllGlobals();
  });

  it('covers backend-published events', () => {
    expect(WEBSOCKET_EVENT_TYPES).toEqual(
      expect.arrayContaining([
        'entity_created',
        'entity_updated',
        'entity_deleted',
        'entity_pending',
        'search_complete',
        'crawl_started',
        'crawl_progress',
        'crawl_complete',
        'crawl_sync_complete',
        'health_update',
        'permission_changed',
        'note_pending',
        'note_created',
        'backup_started',
        'backup_complete',
        'backup_failed',
        'graph_updated',
        'question_answered',
        'source_import_updated',
        'raw_capture_changed',
      ])
    );
  });

  it('accepts known events and rejects unknown events', () => {
    expect(isWebSocketEventType('backup_complete')).toBe(true);
    expect(isWebSocketEventType('source_import_updated')).toBe(true);
    expect(isWebSocketEventType('raw_capture_changed')).toBe(true);
    expect(isWebSocketEventType('surprise')).toBe(false);
  });

  it('tracks backend broadcast events separately from local client events', () => {
    expect(WEBSOCKET_BROADCAST_EVENT_TYPES).toContain('raw_capture_changed');
    expect(WEBSOCKET_BROADCAST_EVENT_TYPES).not.toContain('connection_status');
    expect(WEBSOCKET_BROADCAST_EVENT_TYPES).not.toContain('subscribed');
  });

  it('subscribes current broadcast handlers when the socket opens', () => {
    vi.stubGlobal('WebSocket', MockWebSocket);

    wsClient.on('connection_status', vi.fn());
    wsClient.on('raw_capture_changed', vi.fn());
    wsClient.connect();

    const socket = MockWebSocket.instances[0];
    socket.onopen?.();

    expect(socket.send).toHaveBeenCalledWith(
      JSON.stringify({ type: 'subscribe', topics: ['raw_capture_changed'] })
    );
  });

  it('syncs subscription topics when broadcast handlers change', () => {
    vi.stubGlobal('WebSocket', MockWebSocket);
    wsClient.connect();
    const socket = MockWebSocket.instances[0];
    socket.onopen?.();

    const unsubscribeRaw = wsClient.on('raw_capture_changed', vi.fn());
    wsClient.on('entity_updated', vi.fn());

    expect(socket.send).toHaveBeenLastCalledWith(
      JSON.stringify({
        type: 'subscribe',
        topics: ['entity_updated', 'raw_capture_changed'],
      })
    );

    unsubscribeRaw();

    expect(socket.send).toHaveBeenLastCalledWith(
      JSON.stringify({ type: 'subscribe', topics: ['entity_updated'] })
    );
  });
});
