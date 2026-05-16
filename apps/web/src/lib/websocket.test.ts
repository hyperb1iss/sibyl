import { describe, expect, it } from 'vitest';
import { isWebSocketEventType, WEBSOCKET_EVENT_TYPES } from './websocket';

describe('websocket event contract', () => {
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
      ])
    );
  });

  it('accepts known events and rejects unknown events', () => {
    expect(isWebSocketEventType('backup_complete')).toBe(true);
    expect(isWebSocketEventType('source_import_updated')).toBe(true);
    expect(isWebSocketEventType('surprise')).toBe(false);
  });
});
