import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useApplyMemoryCorrection: vi.fn(),
  useMemorySourceInspect: vi.fn(),
  usePreviewMemoryCorrection: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);
vi.mock('sonner', () => ({ toast }));

import { SourceInspectPanel } from './source-inspect-panel';

const source = {
  id: 'raw-1',
  organization_id: 'org-1',
  source_id: 'cli:manual',
  principal_id: 'user-1',
  agent_id: null,
  project_id: 'project-a',
  memory_scope: 'project' as const,
  scope_key: 'project-a',
  review_state: 'pending',
  visibility: {
    content_visible: true,
    content_redacted: false,
    policy_reason: 'same_scope_read_allowed',
  },
  lifecycle: {},
  reflection_findings: [
    {
      id: 'finding-1',
      kind: 'duplicate',
      status: 'active',
      reason: 'matches an existing project decision',
      source_ids: ['raw-1'],
      related_source_ids: ['raw-older'],
    },
  ],
  claim_records: [],
  correction_history: [
    {
      audit_event_id: 'audit-correction',
      action: 'memory.mark_stale',
      policy_reason: 'same_scope_write_allowed',
      created_at: '2026-05-14T12:00:00Z',
    },
  ],
  promotion_state: { state: 'not_promoted' },
  share_state: { state: 'none' },
  entity_type: 'episode',
  title: 'Decision note',
  raw_content: 'Raw memory body with the exact source text.',
  content_redacted: false,
  raw_content_length: 39,
  tags: ['decision'],
  metadata: { adapter_name: 'manual', memory_scope: 'project' },
  provenance: { source: 'cli' },
  capture_surface: 'cli',
  captured_at: '2026-05-14T12:00:00Z',
  created_at: '2026-05-14T12:00:00Z',
  freshness_timestamps: {
    captured_at: '2026-05-14T12:00:00Z',
    created_at: '2026-05-14T12:00:00Z',
  },
  transform_versions: { transform_version: 'v1' },
  policy_allowed: true,
  policy_reason: 'same_scope_read_allowed',
  policy_metadata: { policy_action: 'read' },
  derived_ids: ['episode-1'],
  derived_types: ['graph_entity'],
  derived_records: [
    {
      id: 'episode-1',
      record_type: 'graph_entity',
      source_action: 'memory.reflect.promote',
    },
  ],
  recent_audit_events: [
    {
      id: 'audit-1',
      organization_id: 'org-1',
      user_id: 'user-1',
      action: 'memory.recall',
      memory_scope: 'project',
      scope_key: 'project-a',
      project_id: 'project-a',
      source_surface: 'raw_recall',
      source_ids: ['cli:manual'],
      source_ids_truncated: null,
      derived_ids: ['raw-1'],
      derived_ids_truncated: null,
      policy_allowed: true,
      policy_reason: 'same_scope_read_allowed',
      details: {},
      created_at: '2026-05-14T12:10:00Z',
    },
  ],
  audit_event_count: 1,
  available_actions: [
    { action: 'correction.preview', available: true, preview_required: true },
    { action: 'share.preview', available: true, preview_required: true },
  ],
};

function setupInspect(overrides = {}) {
  hooks.useMemorySourceInspect.mockReturnValue({
    data: { ...source, ...overrides },
    error: null,
    isLoading: false,
    refetch: vi.fn(),
  });
}

describe('SourceInspectPanel', () => {
  const previewMutate = vi.fn();
  const applyMutate = vi.fn();

  beforeEach(() => {
    previewMutate.mockReset();
    applyMutate.mockReset();
    toast.success.mockReset();
    setupInspect();
    hooks.usePreviewMemoryCorrection.mockReturnValue({
      mutateAsync: previewMutate,
      isPending: false,
    });
    hooks.useApplyMemoryCorrection.mockReturnValue({
      mutateAsync: applyMutate,
      isPending: false,
    });
  });

  it('renders source metadata, raw content, derived records, and audit receipts', () => {
    render(<SourceInspectPanel sourceId="raw-1" />);

    expect(screen.getByText('Decision note')).toBeInTheDocument();
    expect(screen.getByText('Raw memory body with the exact source text.')).toBeInTheDocument();
    expect(screen.getByText('Source Metadata')).toBeInTheDocument();
    expect(screen.getByText('Derived Records')).toBeInTheDocument();
    expect(screen.getByText('episode-1')).toBeInTheDocument();
    expect(screen.getByText('Reflection Findings')).toBeInTheDocument();
    expect(screen.getByText('duplicate')).toBeInTheDocument();
    expect(screen.getByText('matches an existing project decision')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'source:raw-1' })).toHaveAttribute(
      'href',
      '/memory/sources/raw-1'
    );
    expect(screen.getByText('Audit Summary')).toBeInTheDocument();
    expect(screen.getAllByText('same_scope_read_allowed').length).toBeGreaterThan(0);
  });

  it('does not render raw text when the API marks content redacted', () => {
    setupInspect({
      content_redacted: true,
      policy_allowed: false,
      policy_reason: 'lifecycle_redacted',
      raw_content: 'SECRET HIDDEN TEXT',
      raw_content_length: 18,
    });

    render(<SourceInspectPanel sourceId="raw-1" />);

    expect(screen.getByText('Raw text hidden')).toBeInTheDocument();
    expect(screen.getByText('Content hidden by policy or lifecycle state.')).toBeInTheDocument();
    expect(screen.queryByText('SECRET HIDDEN TEXT')).not.toBeInTheDocument();
  });

  it('previews before applying a correction', async () => {
    previewMutate.mockResolvedValue({
      allowed: true,
      applied: false,
      source_id: 'raw-1',
      action: 'mark_stale',
      reason: 'same_scope_write_allowed',
      target_review_state: 'stale',
      updated_review_state: null,
      affected_source_ids: ['raw-1'],
      affected_derived_ids: ['episode-1'],
      reversible: true,
      recall_impact: { hidden_count: 1 },
      synthesis_impact: { hidden_count: 1 },
      audit_action: 'memory.mark_stale',
      policy_reasons: ['same_scope_write_allowed'],
      metadata: {},
    });
    applyMutate.mockResolvedValue({
      allowed: true,
      applied: true,
      source_id: 'raw-1',
      action: 'mark_stale',
      reason: 'same_scope_write_allowed',
      target_review_state: 'stale',
      updated_review_state: 'stale',
      affected_source_ids: ['raw-1'],
      affected_derived_ids: ['episode-1'],
      reversible: true,
      recall_impact: { hidden_count: 1 },
      synthesis_impact: { hidden_count: 1 },
      audit_action: 'memory.mark_stale',
      policy_reasons: ['same_scope_write_allowed'],
      metadata: {},
    });

    const { user } = render(<SourceInspectPanel sourceId="raw-1" />);

    await user.click(screen.getByRole('button', { name: /correction/i }));
    await user.click(screen.getByRole('combobox', { name: /action/i }));
    await user.click(await screen.findByRole('option', { name: 'Mark stale' }));
    await user.type(screen.getByRole('textbox', { name: /reason/i }), 'outdated source');
    await user.click(screen.getByRole('button', { name: 'Preview' }));

    await waitFor(() => {
      expect(previewMutate).toHaveBeenCalledWith({
        sourceId: 'raw-1',
        request: expect.objectContaining({
          action: 'mark_stale',
          reason: 'outdated source',
        }),
      });
    });

    await user.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(applyMutate).toHaveBeenCalledWith({
        sourceId: 'raw-1',
        request: expect.objectContaining({
          action: 'mark_stale',
          reason: 'outdated source',
        }),
      });
    });
    expect(toast.success).toHaveBeenCalledWith('Correction applied');
  });
});
