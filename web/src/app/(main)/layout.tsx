import type { ReactNode } from 'react';
import { AsyncBoundary } from '@/components/error-boundary';
import { Header } from '@/components/layout/header';
import { MobileNavProvider } from '@/components/layout/mobile-nav-context';
import { Sidebar } from '@/components/layout/sidebar';
import { OnboardingGate } from '@/components/onboarding';

export default function MainLayout({ children }: { children: ReactNode }) {
  return (
    <MobileNavProvider>
      <OnboardingGate>
        <div className="flex h-dvh overflow-hidden">
          <Sidebar />
          <div className="flex-1 flex flex-col overflow-hidden min-w-0">
            <Header />
            <main className="flex-1 overflow-auto bg-sc-bg-dark p-3 sm:p-4 md:p-6">
              <AsyncBoundary level="page">{children}</AsyncBoundary>
            </main>
          </div>
        </div>
      </OnboardingGate>
    </MobileNavProvider>
  );
}
