'use client';

import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useState } from 'react';
import { ApiKeysStep } from '@/components/setup/steps/api-keys-step';
import type { SetupStatus } from '@/lib/api/admin';
import { useSetupStatus } from '@/lib/hooks/admin';
import { useMe, useUpdatePreferences } from '@/lib/hooks/auth';
import { CompletionStep } from './steps/completion-step';
import { ConnectStep } from './steps/connect-step';
import { ProjectStep } from './steps/project-step';
import { TaskStep } from './steps/task-step';
import { WelcomeStep } from './steps/welcome-step';

type OnboardingStep = 'welcome' | 'models' | 'connect' | 'project' | 'task' | 'complete';

/**
 * Everyone connects their tools. Only an instance admin on a server whose
 * model providers are not ready also sees the keys step: members could not
 * change server settings anyway, and a keyless provider needs no keys at all.
 */
export function onboardingSteps(
  status: SetupStatus | undefined,
  isAdmin: boolean
): OnboardingStep[] {
  const needsModels = isAdmin && status !== undefined && !status.providers_configured;
  return ['welcome', ...(needsModels ? (['models'] as const) : []), 'connect', 'project', 'task'];
}

interface OnboardingWizardProps {
  onComplete: () => void;
}

const slide = {
  initial: { opacity: 0, x: 20 },
  animate: { opacity: 1, x: 0 },
  exit: { opacity: 0, x: -20 },
  transition: { duration: 0.2 },
};

export function OnboardingWizard({ onComplete }: OnboardingWizardProps) {
  const [step, setStep] = useState<OnboardingStep>('welcome');
  const [createdProjectId, setCreatedProjectId] = useState<string | null>(null);
  const updatePreferences = useUpdatePreferences();
  const { data: me } = useMe();
  const { data: setupStatus } = useSetupStatus();
  // The list is fixed at the first step forward taken with the user and status
  // loaded: saving keys refreshes the status, and a rebuilt list would drop the
  // step they are standing on. Until both load, the list may still grow.
  const [frozenSteps, setFrozenSteps] = useState<OnboardingStep[] | null>(null);
  const steps = frozenSteps ?? onboardingSteps(setupStatus, me?.user.is_admin === true);
  const stepIndex = steps.indexOf(step);
  const loaded = me !== undefined && setupStatus !== undefined;

  const goNext = useCallback(() => {
    if (loaded) setFrozenSteps(steps);
    setStep(steps[stepIndex + 1] ?? 'complete');
  }, [loaded, steps, stepIndex]);

  const goBack = useCallback(() => {
    setStep(steps[Math.max(0, stepIndex - 1)]);
  }, [steps, stepIndex]);

  const handleComplete = useCallback(async () => {
    await updatePreferences.mutateAsync({ is_onboarded: true });
    onComplete();
  }, [updatePreferences, onComplete]);

  const handleSkip = useCallback(async () => {
    await handleComplete();
  }, [handleComplete]);

  const handleProjectCreated = useCallback((projectId: string) => {
    setCreatedProjectId(projectId);
    setStep('task');
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="absolute inset-0 bg-sc-bg-dark/95 backdrop-blur-sm"
      />

      {/* Modal */}
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        transition={{ type: 'spring', duration: 0.5 }}
        className="relative w-full max-w-2xl mx-4 max-h-dvh overflow-y-auto"
      >
        <div className="bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl shadow-2xl shadow-black/40 overflow-hidden">
          {/* Progress indicator */}
          {step !== 'complete' && (
            <div className="px-6 pt-5 pb-3 border-b border-sc-fg-subtle/10">
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  {steps.map((s, i) => (
                    <div
                      key={s}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        s === step
                          ? 'bg-sc-purple'
                          : i < stepIndex
                            ? 'bg-sc-purple/60'
                            : 'bg-sc-fg-subtle/30'
                      }`}
                    />
                  ))}
                </div>
                <span className="text-sc-fg-subtle text-xs">
                  Step {stepIndex + 1} of {steps.length}
                </span>
              </div>
            </div>
          )}

          {/* Step content */}
          <AnimatePresence mode="wait">
            {step === 'welcome' && (
              <motion.div key="welcome" {...slide}>
                <WelcomeStep onNext={goNext} onSkip={handleSkip} />
              </motion.div>
            )}

            {step === 'models' && (
              <motion.div key="models" {...slide}>
                <ApiKeysStep
                  initialStatus={setupStatus}
                  onBack={goBack}
                  onValidated={valid => valid && goNext()}
                />
                <div className="-mt-4 pb-6 text-center">
                  <button
                    type="button"
                    onClick={goNext}
                    className="rounded text-sm text-sc-fg-muted transition-colors duration-200 hover:text-sc-fg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
                  >
                    Set up models later
                  </button>
                </div>
              </motion.div>
            )}

            {step === 'connect' && (
              <motion.div key="connect" {...slide}>
                <ConnectStep onBack={goBack} onNext={goNext} onSkip={handleSkip} />
              </motion.div>
            )}

            {step === 'project' && (
              <motion.div key="project" {...slide}>
                <ProjectStep onBack={goBack} onNext={handleProjectCreated} onSkip={handleSkip} />
              </motion.div>
            )}

            {step === 'task' && (
              <motion.div key="task" {...slide}>
                <TaskStep
                  projectId={createdProjectId}
                  onBack={goBack}
                  onNext={() => setStep('complete')}
                  onSkip={handleSkip}
                />
              </motion.div>
            )}

            {step === 'complete' && (
              <motion.div
                key="complete"
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.3 }}
              >
                <CompletionStep onFinish={handleComplete} />
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </motion.div>
    </div>
  );
}
