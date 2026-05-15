import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { SettingsNav } from '@/components/layout/settings-nav';

export const metadata: Metadata = {
  title: 'Settings',
  description: 'Manage your account, preferences, and team settings',
};

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="max-w-6xl mx-auto">
      <div className="flex flex-col md:flex-row gap-8">
        <aside className="w-full md:w-56 shrink-0 md:sticky md:top-6 md:self-start">
          <SettingsNav />
        </aside>
        <main className="flex-1 min-w-0">{children}</main>
      </div>
    </div>
  );
}
