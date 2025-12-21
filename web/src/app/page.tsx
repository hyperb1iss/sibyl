import { Suspense } from 'react';

import { DashboardSkeleton } from '@/components/suspense-boundary';
import { fetchStats } from '@/lib/api-server';
import { DashboardContent } from './dashboard-content';

export default async function DashboardPage() {
  // Server-side fetch for initial stats
  const stats = await fetchStats();

  return (
    <Suspense fallback={<DashboardSkeleton />}>
      <DashboardContent initialStats={stats} />
    </Suspense>
  );
}
