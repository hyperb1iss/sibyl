import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useEntity: vi.fn(),
}));

vi.mock('@/lib/hooks/graph', async () => {
  const actual = await vi.importActual<typeof import('@/lib/hooks/graph')>('@/lib/hooks/graph');
  return {
    ...actual,
    useEntity: hooks.useEntity,
  };
});

import { EntityDetailPanel } from './entity-detail-panel';

describe('EntityDetailPanel', () => {
  beforeEach(() => {
    hooks.useEntity.mockReset();
    hooks.useEntity.mockReturnValue({
      data: {
        id: 'project-1',
        entity_type: 'project',
        name: 'Sibyl Native',
        description: 'Surreal runtime',
        content: '',
        category: null,
        languages: [],
        tags: [],
        metadata: {},
        source_file: null,
        created_at: null,
        updated_at: null,
        related: null,
      },
      isLoading: false,
      error: null,
    });
  });

  it('uses graph-mode entity reads and renders provided graph neighbors', () => {
    render(
      <EntityDetailPanel
        entityId="project-1"
        onClose={vi.fn()}
        queryMode="graph"
        relatedEntities={[
          {
            id: 'pattern-1',
            name: 'Prefer graph-light sidebars',
            entity_type: 'pattern',
            relationship: 'RELATED_TO',
            direction: 'outgoing',
          },
          {
            id: 'note-1',
            name: 'Render graph neighbors locally',
            entity_type: 'note',
            relationship: 'REFERENCES',
            direction: 'incoming',
          },
        ]}
      />
    );

    expect(hooks.useEntity).toHaveBeenCalledWith('project-1', undefined, {
      include_summary: false,
      related_limit: 0,
    });
    expect(screen.getByText('Prefer graph-light sidebars')).toBeInTheDocument();
    expect(screen.getByText(/Connected To/i)).toBeInTheDocument();
    expect(screen.getByText(/Referenced By/i)).toBeInTheDocument();
  });
});
