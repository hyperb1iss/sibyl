import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Suspense } from 'react';

import { EntityDetailSkeleton } from '@/components/suspense-boundary';
import { ColorButton } from '@/components/ui/button';
import { ErrorState } from '@/components/ui/tooltip';
import { fetchEntity } from '@/lib/api-server';
import { EntityDetailContent } from './entity-detail-content';

interface PageProps {
  params: Promise<{ id: string }>;
}

export default async function EntityDetailPage({ params }: PageProps) {
  const { id } = await params;

  try {
    const entity = await fetchEntity(id);

    return (
      <Suspense fallback={<EntityDetailSkeleton />}>
        <EntityDetailContent initialEntity={entity} />
      </Suspense>
    );
  } catch (error) {
    // Check if it's a 404
    if (error instanceof Error && error.message.includes('404')) {
      notFound();
    }

    // Other errors - show error state
    return (
      <ErrorState
        title="Failed to load entity"
        message={error instanceof Error ? error.message : 'An unexpected error occurred'}
        action={
          <Link href="/entities">
            <ColorButton color="purple">← Back to Entities</ColorButton>
          </Link>
        }
      />
    );
  }
}
