import { Suspense } from 'react';

import { DashboardSkeleton } from '@/components/suspense-boundary';
import { fetchStats } from '@/lib/api-server';
import { DashboardContent } from './dashboard-content';

export default async function DashboardPage() {
  // Server-side fetch for initial stats - fallback on auth failure
  let stats = { entity_counts: {}, total_entities: 0 };
  try {
    stats = await fetchStats();
  } catch {
    // Auth or network error - client will retry
  }

  return (
    <Suspense fallback={<DashboardSkeleton />}>
      <DashboardContent initialStats={stats} />
    </Suspense>
  );
}
