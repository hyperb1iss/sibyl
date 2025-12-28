import { Suspense } from 'react';

import { SearchSkeleton } from '@/components/suspense-boundary';
import { fetchSearchResults, fetchStats } from '@/lib/api-server';
import { SearchContent } from './search-content';

interface PageProps {
  searchParams: Promise<{ q?: string }>;
}

export default async function SearchPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const query = params.q || '';

  // Only fetch if there's a query in the URL
  const [initialResults, stats] = await Promise.all([
    query ? fetchSearchResults({ query, limit: 50 }).catch(() => undefined) : undefined,
    fetchStats(),
  ]);

  return (
    <Suspense fallback={<SearchSkeleton />}>
      <SearchContent initialQuery={query} initialResults={initialResults} initialStats={stats} />
    </Suspense>
  );
}
