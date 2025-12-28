import type { ReactNode } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { SettingsNav } from '@/components/layout/settings-nav';

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="max-w-6xl mx-auto space-y-6">
      <PageHeader
        title="Settings"
        description="Manage your account, preferences, and team settings"
      />

      <div className="flex flex-col md:flex-row gap-6">
        {/* Settings sidebar */}
        <aside className="w-full md:w-64 shrink-0">
          <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-3">
            <SettingsNav />
          </div>
        </aside>

        {/* Main content */}
        <main className="flex-1 min-w-0">{children}</main>
      </div>
    </div>
  );
}
