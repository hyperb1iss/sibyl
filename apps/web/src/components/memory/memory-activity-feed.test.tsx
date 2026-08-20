import { describe, expect, it } from 'vitest';
import type { MemoryAuditEvent } from '@/lib/api/memory';
import { render, screen } from '@/test/utils';
import { MemoryActivityFeed } from './memory-activity-feed';

const baseEvent: MemoryAuditEvent = {
  id: 'audit-dream',
  organization_id: 'org-1',
  user_id: 'user-1',
  action: 'memory.reflect.dream_promote',
  memory_scope: 'project',
  scope_key: 'project-a',
  project_id: 'project-a',
  source_surface: 'reflection_dream_cycle',
  source_ids: ['candidate-1', 'raw-source-1', 'raw-source-2'],
  source_ids_truncated: 1,
  derived_ids: ['entity-1'],
  derived_ids_truncated: null,
  policy_allowed: true,
  policy_reason: 'auto_promote_candidate',
  details: { run_id: 'reflection_dream:org-1:run-1' },
  created_at: '2026-05-15T12:00:00Z',
};

describe('MemoryActivityFeed', () => {
  it('renders automatic reflection receipts with source and entity links', () => {
    render(<MemoryActivityFeed events={[baseEvent]} />);

    expect(screen.getByText('Automatic promotion')).toBeInTheDocument();
    expect(screen.getByText('Auto-promote candidate')).toBeInTheDocument();
    expect(screen.getByText('Dream cycle')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'source:candidate-1' })).toHaveAttribute(
      'href',
      '/memory/sources/candidate-1'
    );
    expect(screen.getByRole('link', { name: 'entity:entity-1' })).toHaveAttribute(
      'href',
      '/entities/entity-1'
    );
    expect(screen.getByText('+2 sources')).toBeInTheDocument();
  });

  it('labels automatic exception routing', () => {
    render(
      <MemoryActivityFeed
        events={[
          {
            ...baseEvent,
            id: 'audit-review',
            action: 'memory.reflect.dream_review',
            derived_ids: [],
            policy_allowed: false,
            policy_reason: 'duplicate_candidate',
          },
        ]}
      />
    );

    expect(screen.getByText('Exception routed')).toBeInTheDocument();
    expect(screen.getByText('Duplicate candidate')).toBeInTheDocument();
  });
});
