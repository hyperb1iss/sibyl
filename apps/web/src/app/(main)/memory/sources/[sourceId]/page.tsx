'use client';

import { use } from 'react';
import { useSetBreadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { SourceInspectPanel } from '@/components/memory/source-inspect-panel';

interface PageProps {
  params: Promise<{ sourceId: string }>;
}

export default function MemorySourcePage({ params }: PageProps) {
  const { sourceId } = use(params);
  const decodedSourceId = decodeURIComponent(sourceId);
  const crumbLabel =
    decodedSourceId.length > 28
      ? `${decodedSourceId.slice(0, 14)}…${decodedSourceId.slice(-8)}`
      : decodedSourceId;

  useSetBreadcrumb([
    { label: 'Home', href: '/' },
    { label: 'Memory', href: '/memory' },
    { label: crumbLabel },
  ]);

  return (
    <div className="space-y-4 animate-fade-in">
      <PageHeader
        title="Source Inspect"
        description="Source metadata, visibility, derived records, audit receipts, and lifecycle actions"
      />
      <SourceInspectPanel sourceId={decodedSourceId} />
    </div>
  );
}
