import type { ReactNode } from 'react';
import { MainShell } from '@/components/layout/main-shell';
import { MobileNavProvider } from '@/components/layout/mobile-nav-context';
import { OnboardingGate } from '@/components/onboarding';

export default function MainLayout({ children }: { children: ReactNode }) {
  return (
    <MobileNavProvider>
      <OnboardingGate>
        <MainShell>{children}</MainShell>
      </OnboardingGate>
    </MobileNavProvider>
  );
}
