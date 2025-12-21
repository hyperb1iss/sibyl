import { Suspense } from 'react';

import { EntitiesSkeleton } from '@/components/suspense-boundary';
import { fetchEntities, fetchStats } from '@/lib/api-server';
import { EntitiesContent } from './entities-content';

interface PageProps {
  searchParams: Promise<{ type?: string; page?: string }>;
}

export default async function EntitiesPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const typeFilter = params.type;
  const page = parseInt(params.page || '1', 10);
  const limit = 20;

  // Server-side parallel fetch
  const [entities, stats] = await Promise.all([
    fetchEntities({ entity_type: typeFilter, page, page_size: limit }),
    fetchStats(),
  ]);

  return (
    <Suspense fallback={<EntitiesSkeleton />}>
      <EntitiesContent
        initialEntities={entities}
        initialStats={stats}
        typeFilter={typeFilter}
        page={page}
      />
    </Suspense>
  );
}
