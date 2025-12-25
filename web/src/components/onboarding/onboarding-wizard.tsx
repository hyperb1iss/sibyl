'use client';

import { AnimatePresence, motion } from 'motion/react';
import { useCallback, useState } from 'react';
import { useUpdatePreferences } from '@/lib/hooks';
import { CompletionStep } from './steps/completion-step';
import { ProjectStep } from './steps/project-step';
import { TaskStep } from './steps/task-step';
import { WelcomeStep } from './steps/welcome-step';

type OnboardingStep = 'welcome' | 'project' | 'task' | 'complete';

interface OnboardingWizardProps {
  onComplete: () => void;
}

export function OnboardingWizard({ onComplete }: OnboardingWizardProps) {
  const [step, setStep] = useState<OnboardingStep>('welcome');
  const [createdProjectId, setCreatedProjectId] = useState<string | null>(null);
  const updatePreferences = useUpdatePreferences();

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

  const handleTaskCreated = useCallback(() => {
    setStep('complete');
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
        className="relative w-full max-w-2xl mx-4"
      >
        <div className="bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-2xl shadow-2xl shadow-black/40 overflow-hidden">
          {/* Progress indicator */}
          {step !== 'complete' && (
            <div className="px-6 pt-5 pb-3 border-b border-sc-fg-subtle/10">
              <div className="flex items-center justify-between">
                <div className="flex gap-2">
                  {(['welcome', 'project', 'task'] as const).map((s, i) => (
                    <div
                      key={s}
                      className={`w-2 h-2 rounded-full transition-colors ${
                        s === step
                          ? 'bg-sc-purple'
                          : i < ['welcome', 'project', 'task'].indexOf(step)
                            ? 'bg-sc-purple/60'
                            : 'bg-sc-fg-subtle/30'
                      }`}
                    />
                  ))}
                </div>
                <span className="text-sc-fg-subtle text-xs">
                  Step {['welcome', 'project', 'task'].indexOf(step) + 1} of 3
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
                <WelcomeStep onNext={() => setStep('project')} onSkip={handleSkip} />
              </motion.div>
            )}

            {step === 'project' && (
              <motion.div
                key="project"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                transition={{ duration: 0.2 }}
              >
                <ProjectStep
                  onBack={() => setStep('welcome')}
                  onNext={handleProjectCreated}
                  onSkip={handleSkip}
                />
              </motion.div>
            )}

            {step === 'task' && (
              <motion.div
                key="task"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                transition={{ duration: 0.2 }}
              >
                <TaskStep
                  projectId={createdProjectId}
                  onBack={() => setStep('project')}
                  onNext={handleTaskCreated}
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
