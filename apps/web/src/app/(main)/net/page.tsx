import type { Metadata } from 'next';
import { Suspense } from 'react';
import { NetSkeleton } from '@/components/suspense-boundary';
import { NetContent } from './net-content';

export const metadata: Metadata = {
  title: 'Net',
  description: 'What agents are saying to each other on sibylnet',
};

export default function NetPage() {
  return (
    <Suspense fallback={<NetSkeleton />}>
      <NetContent />
    </Suspense>
  );
}
