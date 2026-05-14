'use client';

import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { MemoryHome } from '@/components/memory/memory-home';

export function MemoryContent() {
  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb />
      <PageHeader
        title="Memory Cockpit"
        description="Operate captures, reviews, recalls, imports, and agent access from one control surface"
      />
      <MemoryHome />
    </div>
  );
}
