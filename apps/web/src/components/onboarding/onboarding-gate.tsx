'use client';

import { type ReactNode, useState } from 'react';
import { usePreferences } from '@/lib/hooks';
import { OnboardingWizard } from './onboarding-wizard';

interface OnboardingGateProps {
  children: ReactNode;
}

/**
 * Wraps children and shows onboarding wizard for first-time users.
 * Checks `is_onboarded` preference to determine if wizard should show.
 */
export function OnboardingGate({ children }: OnboardingGateProps) {
  const { data: prefs, isLoading } = usePreferences();
  const [dismissed, setDismissed] = useState(false);

  // Show wizard if user hasn't completed onboarding and hasn't dismissed it
  const showWizard = !isLoading && prefs && !prefs.preferences?.is_onboarded && !dismissed;

  return (
    <>
      {children}
      {showWizard && <OnboardingWizard onComplete={() => setDismissed(true)} />}
    </>
  );
}
