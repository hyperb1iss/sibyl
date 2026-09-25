'use client';

import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useState } from 'react';
import type { SetupStatus } from '@/lib/api/admin';
import { AdminAccountStep } from './steps/admin-account-step';
import { ApiKeysStep } from './steps/api-keys-step';
import { ConnectStep } from './steps/connect-step';
import { WelcomeStep } from './steps/welcome-step';

type SetupStep = 'welcome' | 'api-keys' | 'admin' | 'connect';

interface SetupWizardProps {
  initialStatus: SetupStatus | undefined;
  onComplete: () => void;
}

const STEP_STORAGE_KEY = 'sibyl-setup-step';

/** The keys step exists only while the server has no ready model provider. */
export function setupSteps(status: SetupStatus | undefined): SetupStep[] {
  return status?.providers_configured
    ? ['welcome', 'admin', 'connect']
    : ['welcome', 'api-keys', 'admin', 'connect'];
}

function getStoredStep(steps: SetupStep[]): SetupStep {
  if (typeof window === 'undefined') return 'welcome';
  try {
    const stored = sessionStorage.getItem(STEP_STORAGE_KEY);
    if (stored && steps.includes(stored as SetupStep)) {
      return stored as SetupStep;
    }
  } catch {
    // sessionStorage may throw in restricted browsers (e.g., Safari private mode)
  }
  return 'welcome';
}

export function SetupWizard({ initialStatus, onComplete }: SetupWizardProps) {
  // Fixed at mount: saving keys refreshes the status, and a list rebuilt from it
  // would drop the step the owner is standing on.
  const [steps] = useState(() => setupSteps(initialStatus));
  const [step, setStep] = useState<SetupStep>(() => getStoredStep(steps));

  // Persist step to sessionStorage so tab switches don't reset progress
  useEffect(() => {
    try {
      sessionStorage.setItem(STEP_STORAGE_KEY, step);
    } catch {
      // sessionStorage may throw in restricted browsers
    }
  }, [step]);

  // Clear stored step on completion
  const handleComplete = useCallback(() => {
    try {
      sessionStorage.removeItem(STEP_STORAGE_KEY);
    } catch {
      // sessionStorage may throw in restricted browsers
    }
    onComplete();
  }, [onComplete]);

  const currentIndex = steps.indexOf(step);
  const isLastStep = step === 'connect';

  const handleNext = useCallback(() => {
    const nextIndex = currentIndex + 1;
    if (nextIndex < steps.length) {
      setStep(steps[nextIndex]);
    }
  }, [currentIndex, steps]);

  const handleBack = useCallback(() => {
    const prevIndex = currentIndex - 1;
    if (prevIndex >= 0) {
      setStep(steps[prevIndex]);
    }
  }, [currentIndex, steps]);

  const handleApiKeysValidated = useCallback(
    (valid: boolean) => {
      if (valid) {
        handleNext();
      }
    },
    [handleNext]
  );

  const handleAccountCreated = useCallback(() => {
    handleNext();
  }, [handleNext]);

  return (
    <div className="w-full max-w-2xl">
      <div className="bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl shadow-card-elevated overflow-hidden">
        {/* Progress indicator */}
        {!isLastStep && (
          <div className="px-6 pt-5 pb-3 border-b border-sc-fg-subtle/10">
            <div className="flex items-center justify-between">
              <div className="flex gap-2">
                {steps.slice(0, -1).map((s, i) => (
                  <div
                    key={s}
                    className={`w-2 h-2 rounded-full transition-colors duration-200 ${
                      s === step
                        ? 'bg-sc-purple'
                        : i < currentIndex
                          ? 'bg-sc-purple/60'
                          : 'bg-sc-fg-subtle/30'
                    }`}
                  />
                ))}
              </div>
              <span className="text-sc-fg-subtle text-xs">
                Step {currentIndex + 1} of {steps.length - 1}
              </span>
            </div>
          </div>
        )}

        {/* Step content */}
        <AnimatePresence mode="wait">
          {step === 'welcome' && (
            <motion.div
              key="welcome"
              initial={{ opacity: 0, x: 20 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -20 }}
              transition={{ duration: 0.2 }}
            >
              <WelcomeStep
                onNext={handleNext}
                configuredProviders={
                  initialStatus?.providers_configured ? initialStatus.configured_providers : []
                }
              />
            </motion.div>
          )}

          {step === 'api-keys' && (
            <motion.div
              key="api-keys"
              initial={{ opacity: 0, x: 20 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -20 }}
              transition={{ duration: 0.2 }}
            >
              <ApiKeysStep
                initialStatus={initialStatus}
                onBack={handleBack}
                onValidated={handleApiKeysValidated}
              />
            </motion.div>
          )}

          {step === 'admin' && (
            <motion.div
              key="admin"
              initial={{ opacity: 0, x: 20 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -20 }}
              transition={{ duration: 0.2 }}
            >
              <AdminAccountStep onBack={handleBack} onAccountCreated={handleAccountCreated} />
            </motion.div>
          )}

          {step === 'connect' && (
            <motion.div
              key="connect"
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.3 }}
            >
              <ConnectStep onFinish={handleComplete} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
