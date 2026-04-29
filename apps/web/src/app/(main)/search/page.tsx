import type { Metadata } from 'next';
import { Suspense } from 'react';

import { SearchSkeleton } from '@/components/suspense-boundary';
import { fetchSearchResults, fetchStats } from '@/lib/api-server';
import { SearchContent } from './search-content';

export const metadata: Metadata = {
  title: 'Search',
  description: 'Semantic search across your knowledge graph',
};

interface PageProps {
  searchParams: Promise<{ q?: string }>;
}

export default async function SearchPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const query = params.q || '';

  // Only fetch if there's a query in the URL
  const [initialResults, stats] = await Promise.all([
    query
      ? fetchSearchResults({
          query,
          limit: 50,
          include_documents: false,
          include_graph: true,
        }).catch(() => undefined)
      : undefined,
    fetchStats().catch(() => undefined),
  ]);

  return (
    <Suspense fallback={<SearchSkeleton />}>
      <SearchContent initialQuery={query} initialResults={initialResults} initialStats={stats} />
    </Suspense>
  );
}
