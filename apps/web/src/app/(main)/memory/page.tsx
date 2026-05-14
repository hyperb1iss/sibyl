import type { Metadata } from 'next';
import { Suspense } from 'react';

import { LoadingState } from '@/components/ui/spinner';
import { MemoryContent } from './memory-content';

export const metadata: Metadata = {
  title: 'Memory',
  description: 'Memory cockpit for captures, review actions, recalls, and agent access',
};

export default function MemoryPage() {
  return (
    <Suspense fallback={<LoadingState message="Loading memory cockpit..." />}>
      <MemoryContent />
    </Suspense>
  );
}
