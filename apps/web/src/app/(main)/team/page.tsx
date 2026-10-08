import type { Metadata } from 'next';
import { Suspense } from 'react';

import { TeamSkeleton } from '@/components/suspense-boundary';
import { TeamContent } from './team-content';

export const metadata: Metadata = {
  title: 'Team',
  description: 'Who on the team did what, and when',
};

export default function TeamPage() {
  return (
    <Suspense fallback={<TeamSkeleton />}>
      <TeamContent />
    </Suspense>
  );
}
