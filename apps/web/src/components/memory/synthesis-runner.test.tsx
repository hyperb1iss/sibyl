import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useSynthesisDraft: vi.fn(),
  useSynthesisPlan: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);
vi.mock('sonner', () => ({ toast }));
vi.mock('@/components/ui/markdown', () => ({
  Markdown: ({ content }: { content: string }) => <div>{content}</div>,
}));

import { SynthesisRunner } from './synthesis-runner';

const planResponse = {
  run_id: 'synthesis-run-1',
  status: 'planned' as const,
  request: {
    goal: 'Summarize the roadmap',
    output_type: 'roadmap' as const,
  },
  outline: {
    title: 'Roadmap Synthesis',
    output_type: 'roadmap' as const,
    audience: 'maintainers',
    sections: [
      {
        section_id: 'section-1',
        title: 'Recommended Path',
        prompt: 'Summarize the next implementation path.',
        source_query: 'roadmap next',
        source_ids: ['raw-1', 'raw-2'],
        gaps: [],
      },
    ],
  },
  source_packs: [
    {
      section_id: 'section-1',
      title: 'Recommended Path',
      query: 'roadmap next',
      source_ids: ['raw-1', 'raw-2'],
      sources: [
        {
          id: 'raw-1',
          type: 'episode',
          name: 'Roadmap assessment',
          content_preview: 'Memory cockpit comes before release gates.',
          score: 0.92,
          source: 'memory',
          origin: 'raw',
          relation: null,
          metadata: {},
        },
      ],
      hidden_count: 0,
      redaction_count: 0,
      freshness: {},
      unresolved_claims: [],
    },
  ],
  verification: {
    status: 'pass' as const,
    source_count: 2,
    gap_count: 0,
    gaps: [],
  },
};

const draftResponse = {
  ...planResponse,
  status: 'verified' as const,
  artifact: {
    artifact_id: 'artifact-1',
    format: 'markdown' as const,
    title: 'Roadmap Synthesis',
    markdown: '## Roadmap\n\nShip the unified memory cockpit.',
    json_payload: {},
    source_ids: ['raw-1', 'raw-2'],
    section_source_ids: { 'section-1': ['raw-1', 'raw-2'] },
    generated_text_hash: 'sha256:abc',
    verification: planResponse.verification,
    remembered_memory_id: 'raw-remembered',
    remembered_source_id: 'source-remembered',
  },
};

describe('SynthesisRunner', () => {
  beforeEach(() => {
    class MockResizeObserver {
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    }

    global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
    toast.success.mockReset();
    toast.error.mockReset();
    hooks.useSynthesisPlan.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue(planResponse),
      isPending: false,
    });
    hooks.useSynthesisDraft.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue(draftResponse),
      isPending: false,
    });
  });

  it('plans a synthesis run and renders outline, sources, and verification', async () => {
    const { user } = render(<SynthesisRunner />);

    await user.type(screen.getByLabelText(/goal/i), 'Summarize the roadmap');
    await user.click(screen.getByRole('button', { name: /plan/i }));

    expect(hooks.useSynthesisPlan().mutateAsync).toHaveBeenCalledWith(
      expect.objectContaining({
        goal: 'Summarize the roadmap',
        output_type: 'documentation',
        depth: 'standard',
      })
    );
    expect(await screen.findByDisplayValue('Recommended Path')).toBeInTheDocument();
    expect(screen.getByText('Roadmap assessment')).toBeInTheDocument();
    expect(screen.getByText('Verification')).toBeInTheDocument();
  });

  it('drafts with the reviewed outline and remember settings', async () => {
    const planMutation = vi.fn().mockResolvedValue(planResponse);
    const draftMutation = vi.fn().mockResolvedValue(draftResponse);
    hooks.useSynthesisPlan.mockReturnValue({ mutateAsync: planMutation, isPending: false });
    hooks.useSynthesisDraft.mockReturnValue({ mutateAsync: draftMutation, isPending: false });

    const { user } = render(<SynthesisRunner />);

    fireEvent.change(screen.getByLabelText(/goal/i), {
      target: { value: 'Summarize the roadmap' },
    });
    await user.click(screen.getByLabelText('Remember draft'));
    fireEvent.change(screen.getByLabelText('Scope Key'), {
      target: { value: 'project-sibyl' },
    });
    await user.click(screen.getByRole('button', { name: /plan/i }));
    await user.clear(await screen.findByDisplayValue('Recommended Path'));
    fireEvent.change(screen.getByLabelText(/title/i), {
      target: { value: 'Unified Memory UX' },
    });
    fireEvent.change(screen.getByLabelText(/required sources/i), {
      target: { value: 'raw-1, raw-2' },
    });
    await user.click(screen.getByRole('button', { name: /draft/i }));

    expect(draftMutation).toHaveBeenCalledWith(
      expect.objectContaining({
        goal: 'Summarize the roadmap',
        remember: true,
        memory_scope: 'private',
        scope_key: 'project-sibyl',
        tags: ['synthesis'],
        required_sections: [
          expect.objectContaining({
            title: 'Unified Memory UX',
            required_source_ids: ['raw-1', 'raw-2'],
          }),
        ],
      })
    );
    expect(await screen.findByText(/Ship the unified memory cockpit/i)).toBeInTheDocument();
    expect(screen.getByText('remembered')).toBeInTheDocument();
  });
});
