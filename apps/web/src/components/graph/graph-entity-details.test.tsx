import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@/test/utils';
import { GraphEntityDetails } from './graph-entity-details';

vi.mock('./entity-detail-panel', () => ({
  EntityDetailPanel: ({
    entityId,
    queryMode,
    variant,
  }: {
    entityId: string;
    queryMode: string;
    variant?: string;
  }) => (
    <div
      data-testid="entity-detail-panel"
      data-entity-id={entityId}
      data-query-mode={queryMode}
      data-variant={variant ?? 'default'}
    />
  ),
}));

describe('GraphEntityDetails', () => {
  it('keeps desktop details inline with graph query semantics', () => {
    render(
      <GraphEntityDetails
        entityId="entity:desktop"
        isMobile={false}
        onClose={vi.fn()}
        relatedEntities={[]}
      />
    );

    expect(screen.queryByRole('button', { name: 'Close panel' })).not.toBeInTheDocument();
    expect(screen.getByTestId('entity-detail-panel')).toHaveAttribute(
      'data-entity-id',
      'entity:desktop'
    );
    expect(screen.getByTestId('entity-detail-panel')).toHaveAttribute('data-query-mode', 'graph');
    expect(screen.getByTestId('entity-detail-panel')).toHaveAttribute('data-variant', 'default');
  });

  it('renders the mobile sheet and closes it by pointer or Escape', async () => {
    const onClose = vi.fn();
    const { user } = render(
      <GraphEntityDetails
        entityId="entity:mobile"
        isMobile
        onClose={onClose}
        relatedEntities={[]}
      />
    );
    const closePanel = screen.getByRole('button', { name: 'Close panel' });

    expect(screen.getByTestId('entity-detail-panel')).toHaveAttribute('data-variant', 'sheet');
    await user.click(closePanel);
    fireEvent.keyDown(closePanel, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
