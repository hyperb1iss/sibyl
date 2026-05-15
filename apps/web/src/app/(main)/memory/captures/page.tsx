import { RawCaptureReview } from '@/components/memory/raw-capture-review';
import { Database, FileText } from '@/components/ui/icons';

export default function MemoryCapturesPage() {
  return (
    <RawCaptureReview
      basePath="/memory/captures"
      title="Memory Captures"
      description="Review raw captures, graph linkage, and queued memory actions"
      breadcrumbItems={[
        { label: 'Home', href: '/' },
        { label: 'Memory', href: '/memory', icon: Database },
        { label: 'Captures', icon: FileText },
      ]}
    />
  );
}
