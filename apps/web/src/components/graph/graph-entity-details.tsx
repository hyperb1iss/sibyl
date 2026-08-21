'use client';

import type { RelatedEntitySummary } from '@/lib/api';
import { EntityDetailPanel } from './entity-detail-panel';

interface GraphEntityDetailsProps {
  entityId: string;
  isMobile: boolean;
  onClose: () => void;
  relatedEntities: RelatedEntitySummary[];
}

export function GraphEntityDetails({
  entityId,
  isMobile,
  onClose,
  relatedEntities,
}: GraphEntityDetailsProps) {
  if (!isMobile) {
    return (
      <div className="hidden md:block">
        <EntityDetailPanel
          entityId={entityId}
          onClose={onClose}
          queryMode="graph"
          relatedEntities={relatedEntities}
        />
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 md:hidden">
      <button
        type="button"
        className="absolute inset-0 bg-sc-bg-dark/80 cursor-default"
        onClick={onClose}
        onKeyDown={event => event.key === 'Escape' && onClose()}
        aria-label="Close panel"
      />
      <div className="absolute bottom-0 left-0 right-0 max-h-[70vh] bg-sc-bg-base rounded-t-2xl overflow-hidden animate-slide-up">
        <div className="flex justify-center py-2">
          <div className="w-10 h-1 bg-sc-fg-subtle/30 rounded-full" />
        </div>
        <EntityDetailPanel
          entityId={entityId}
          onClose={onClose}
          variant="sheet"
          queryMode="graph"
          relatedEntities={relatedEntities}
        />
      </div>
    </div>
  );
}
