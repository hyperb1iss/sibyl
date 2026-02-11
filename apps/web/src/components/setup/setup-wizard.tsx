'use client';

import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useEffect, useState } from 'react';
import type { SetupStatus } from '@/lib/api';
import { AdminAccountStep } from './steps/admin-account-step';
import { ApiKeysStep } from './steps/api-keys-step';
import { ConnectClaudeStep } from './steps/connect-claude-step';
import { WelcomeStep } from './steps/welcome-step';

type SetupStep = 'welcome' | 'api-keys' | 'admin' | 'connect';

interface SetupWizardProps {
  initialStatus: SetupStatus | undefined;
  onComplete: () => void;
}

const STEPS: SetupStep[] = ['welcome', 'api-keys', 'admin', 'connect'];
const STEP_STORAGE_KEY = 'sibyl-setup-step';

function getStoredStep(): SetupStep {
  if (typeof window === 'undefined') return 'welcome';
  const stored = sessionStorage.getItem(STEP_STORAGE_KEY);
  if (stored && STEPS.includes(stored as SetupStep)) {
    return stored as SetupStep;
  }
  return 'welcome';
}

export function SetupWizard({ initialStatus, onComplete }: SetupWizardProps) {
  const [step, setStep] = useState<SetupStep>(getStoredStep);

  // Persist step to sessionStorage so tab switches don't reset progress
  useEffect(() => {
    sessionStorage.setItem(STEP_STORAGE_KEY, step);
  }, [step]);

  // Clear stored step on completion
  const handleComplete = useCallback(() => {
    sessionStorage.removeItem(STEP_STORAGE_KEY);
    onComplete();
  }, [onComplete]);

  const currentIndex = STEPS.indexOf(step);
  const isLastStep = step === 'connect';

  const handleNext = useCallback(() => {
    const nextIndex = currentIndex + 1;
    if (nextIndex < STEPS.length) {
      setStep(STEPS[nextIndex]);
    }
  }, [currentIndex]);

  const handleBack = useCallback(() => {
    const prevIndex = currentIndex - 1;
    if (prevIndex >= 0) {
      setStep(STEPS[prevIndex]);
    }
  }, [currentIndex]);

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
      <div className="bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-2xl shadow-2xl shadow-black/40 overflow-hidden">
        {/* Progress indicator */}
        {!isLastStep && (
          <div className="px-6 pt-5 pb-3 border-b border-sc-fg-subtle/10">
            <div className="flex items-center justify-between">
              <div className="flex gap-2">
                {STEPS.slice(0, -1).map((s, i) => (
                  <div
                    key={s}
                    className={`w-2 h-2 rounded-full transition-colors ${
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
                Step {currentIndex + 1} of {STEPS.length - 1}
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
              <WelcomeStep onNext={handleNext} />
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
              <ConnectClaudeStep onFinish={handleComplete} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
