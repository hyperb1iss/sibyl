import type { Metadata } from 'next';
import { Suspense } from 'react';

import { EntitiesSkeleton } from '@/components/suspense-boundary';
import type { EntitySortField, SortOrder } from '@/lib/api';
import { fetchEntities, fetchStats } from '@/lib/api-server';
import { EntitiesContent } from './entities-content';

export const metadata: Metadata = {
  title: 'Entities',
  description: 'Browse and search knowledge graph entities',
};

interface PageProps {
  searchParams: Promise<{
    type?: string;
    search?: string;
    projects?: string; // Comma-separated project IDs from project context
    page?: string;
    sort_by?: EntitySortField;
    sort_order?: SortOrder;
  }>;
}

const VALID_SORT_FIELDS: EntitySortField[] = ['name', 'created_at', 'updated_at', 'entity_type'];
const VALID_SORT_ORDERS: SortOrder[] = ['asc', 'desc'];

export default async function EntitiesPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const typeFilter = params.type;
  const search = params.search || '';
  const projectIds = params.projects ? params.projects.split(',').filter(Boolean) : undefined;
  const page = parseInt(params.page || '1', 10);
  const limit = 20;

  // Validate and default sort params
  const sortBy: EntitySortField = VALID_SORT_FIELDS.includes(params.sort_by as EntitySortField)
    ? (params.sort_by as EntitySortField)
    : 'updated_at';
  const sortOrder: SortOrder = VALID_SORT_ORDERS.includes(params.sort_order as SortOrder)
    ? (params.sort_order as SortOrder)
    : 'desc';

  // Server-side parallel fetch
  const [entities, stats] = await Promise.all([
    fetchEntities({
      entity_type: typeFilter,
      search: search || undefined,
      project_ids: projectIds,
      page,
      page_size: limit,
      sort_by: sortBy,
      sort_order: sortOrder,
    }),
    fetchStats(),
  ]);

  return (
    <Suspense fallback={<EntitiesSkeleton />}>
      <EntitiesContent
        initialEntities={entities}
        initialStats={stats}
        typeFilter={typeFilter}
        search={search}
        page={page}
        sortBy={sortBy}
        sortOrder={sortOrder}
      />
    </Suspense>
  );
}
