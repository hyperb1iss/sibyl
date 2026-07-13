import type { Metadata } from 'next';
import { Suspense } from 'react';

import { DashboardSkeleton } from '@/components/suspense-boundary';
import type { StatsResponse } from '@/lib/api';
import { fetchStats } from '@/lib/api-server';
import { DashboardContent } from './dashboard-content';

export const metadata: Metadata = {
  title: 'Dashboard',
  description: 'Knowledge graph overview and stats',
};

export default async function DashboardPage() {
  let stats: StatsResponse | undefined;
  try {
    stats = await fetchStats();
  } catch {
    // Client-side auth recovery owns stale access tokens.
  }

  return (
    <Suspense fallback={<DashboardSkeleton />}>
      <DashboardContent initialStats={stats} />
    </Suspense>
  );
}
