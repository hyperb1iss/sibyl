import type { Metadata } from 'next';
import { Suspense } from 'react';

import { MemoryHomeSkeleton } from '@/components/suspense-boundary';
import { MemoryContent } from './memory-content';

export const metadata: Metadata = {
  title: 'Memory',
  description:
    'Memory workspace for saved captures, optional inspection, recalls, and agent access',
};

export default function MemoryPage() {
  return (
    <Suspense fallback={<MemoryHomeSkeleton />}>
      <MemoryContent />
    </Suspense>
  );
}
