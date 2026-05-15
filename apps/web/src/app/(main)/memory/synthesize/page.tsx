'use client';

import { useSetBreadcrumb } from '@/components/layout/breadcrumb';
import { SynthesisRunner } from '@/components/memory/synthesis-runner';
import { Database, FileText } from '@/components/ui/icons';

export default function MemorySynthesizePage() {
  useSetBreadcrumb([
    { label: 'Home', href: '/' },
    { label: 'Memory', href: '/memory', icon: Database },
    { label: 'Synthesize', icon: FileText },
  ]);

  return (
    <div className="space-y-4 animate-fade-in">
      <SynthesisRunner />
    </div>
  );
}
