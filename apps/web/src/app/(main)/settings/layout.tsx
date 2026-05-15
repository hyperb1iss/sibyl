import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { SettingsNav } from '@/components/layout/settings-nav';

export const metadata: Metadata = {
  title: 'Settings',
  description: 'Manage your account, preferences, and team settings',
};

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto max-w-6xl">
      <div className="flex flex-col gap-8 md:flex-row">
        <aside className="w-full shrink-0 md:sticky md:top-0 md:w-56 md:self-start md:pt-2">
          <SettingsNav />
        </aside>
        <div className="min-h-[calc(100vh-8rem)] min-w-0 flex-1">{children}</div>
      </div>
    </div>
  );
}
