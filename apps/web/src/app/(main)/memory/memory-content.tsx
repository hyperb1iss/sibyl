'use client';

import { Breadcrumb } from '@/components/layout/breadcrumb';
import { MemoryHome } from '@/components/memory/memory-home';

export function MemoryContent() {
  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb />
      <MemoryHome />
    </div>
  );
}
