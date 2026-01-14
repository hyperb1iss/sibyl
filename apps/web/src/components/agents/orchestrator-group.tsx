'use client';

import Link from 'next/link';
import { memo, useState } from 'react';
import {
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Pause,
  Play,
  XmarkCircle,
} from '@/components/ui/icons';
import type { Agent, TaskOrchestrator } from '@/lib/api';
import {
  AGENT_STATUS_CONFIG,
  type AgentStatusType,
  formatDistanceToNow,
  ORCHESTRATOR_PHASE_CONFIG,
  type OrchestratorPhaseType,
} from '@/lib/constants';

// =============================================================================
// Types
// =============================================================================

interface OrchestratorGroupProps {
  orchestrator: TaskOrchestrator;
  worker: Agent | null;
  taskName?: string;
  onPause: (id: string) => void;
  onResume: (id: string) => void;
  onApprove?: (id: string) => void;
  onReject?: (id: string) => void;
}

// =============================================================================
// Quality Gate Badge
// =============================================================================

const GATE_LABELS: Record<string, string> = {
  lint: 'Lint',
  typecheck: 'Types',
  test: 'Test',
  ai_review: 'AI Review',
  security_scan: 'Security',
  human_review: 'Human',
};

const GatePipeline = memo(function GatePipeline({
  gates,
  currentPhase,
}: {
  gates: string[];
  currentPhase: string;
}) {
  // Determine which gates have been passed based on phase
  const phaseOrder = ['implement', 'review', 'rework', 'human_review', 'merge'];
  const currentPhaseIndex = phaseOrder.indexOf(currentPhase);

  return (
    <div className="flex items-center gap-1">
      {gates.map((gate, index) => {
        // Simple heuristic: gates before current phase are "passed"
        const isPassed = currentPhase === 'merge' || currentPhaseIndex > 1;
        const isCurrent = currentPhase === 'review' && index === gates.length - 1;

        return (
          <span
            key={gate}
            className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${
              isPassed
                ? 'bg-sc-green/20 text-sc-green border border-sc-green/30'
                : isCurrent
                  ? 'bg-sc-purple/20 text-sc-purple border border-sc-purple/30 animate-pulse'
                  : 'bg-sc-fg-subtle/10 text-sc-fg-subtle border border-sc-fg-subtle/20'
            }`}
            title={GATE_LABELS[gate] ?? gate}
          >
            {GATE_LABELS[gate] ?? gate}
          </span>
        );
      })}
    </div>
  );
});

// =============================================================================
// Phase Progress Pipeline
// =============================================================================

const PhasePipeline = memo(function PhasePipeline({ currentPhase }: { currentPhase: string }) {
  const phases: OrchestratorPhaseType[] = [
    'implement',
    'review',
    'rework',
    'human_review',
    'merge',
  ];
  const currentIndex = phases.indexOf(currentPhase as OrchestratorPhaseType);

  return (
    <div className="flex items-center gap-0.5">
      {phases.map((phase, index) => {
        const config = ORCHESTRATOR_PHASE_CONFIG[phase];
        const isActive = phase === currentPhase;
        const isPast = index < currentIndex;
        // Skip rework if we haven't hit it
        if (phase === 'rework' && currentPhase !== 'rework' && currentIndex < 2) return null;

        return (
          <div key={phase} className="flex items-center">
            {index > 0 && phase !== 'rework' && (
              <div className={`w-4 h-px ${isPast ? 'bg-sc-green' : 'bg-sc-fg-subtle/30'}`} />
            )}
            <div
              className={`
                flex items-center justify-center w-6 h-6 rounded-full text-xs
                transition-all duration-200
                ${
                  isActive
                    ? `${config.bgClass} ${config.textClass} ring-2 ring-offset-1 ring-offset-sc-bg-base`
                    : isPast
                      ? 'bg-sc-green/20 text-sc-green'
                      : 'bg-sc-fg-subtle/10 text-sc-fg-subtle'
                }
              `}
              style={
                isActive ? ({ '--tw-ring-color': config.color } as React.CSSProperties) : undefined
              }
              title={config.label}
            >
              {isPast ? '✓' : config.icon}
            </div>
          </div>
        );
      })}
    </div>
  );
});

// =============================================================================
// Worker Card (simplified)
// =============================================================================

const WorkerCard = memo(function WorkerCard({ agent }: { agent: Agent }) {
  const statusConfig =
    AGENT_STATUS_CONFIG[agent.status as AgentStatusType] ?? AGENT_STATUS_CONFIG.working;

  return (
    <Link
      href={`/agents/${agent.id}`}
      className="block ml-6 pl-4 py-2 border-l-2 border-sc-purple/30 hover:border-sc-purple/60 transition-colors"
    >
      <div className="flex items-center gap-2">
        <span
          className={`w-2 h-2 rounded-full ${
            ['working', 'initializing'].includes(agent.status)
              ? 'bg-sc-purple animate-pulse'
              : agent.status === 'completed'
                ? 'bg-sc-green'
                : agent.status === 'failed'
                  ? 'bg-sc-red'
                  : 'bg-sc-fg-subtle'
          }`}
        />
        <span className="text-sm text-sc-fg-primary truncate">{agent.name}</span>
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded ${statusConfig.bgClass} ${statusConfig.textClass}`}
        >
          {statusConfig.label}
        </span>
        {agent.last_heartbeat && (
          <span className="text-[10px] text-sc-fg-subtle ml-auto">
            {formatDistanceToNow(agent.last_heartbeat)}
          </span>
        )}
      </div>
    </Link>
  );
});

// =============================================================================
// Orchestrator Group Card
// =============================================================================

export const OrchestratorGroupCard = memo(function OrchestratorGroupCard({
  orchestrator,
  worker,
  taskName,
  onPause,
  onResume,
  onApprove,
  onReject,
}: OrchestratorGroupProps) {
  const [isExpanded, setIsExpanded] = useState(true);

  const phaseConfig =
    ORCHESTRATOR_PHASE_CONFIG[orchestrator.current_phase as OrchestratorPhaseType] ??
    ORCHESTRATOR_PHASE_CONFIG.implement;

  const isActive = ['implementing', 'reviewing', 'reworking'].includes(orchestrator.status);
  const isPaused = orchestrator.status === 'paused';
  const isHumanReview = orchestrator.status === 'human_review';
  const isComplete = orchestrator.status === 'complete';
  const isFailed = orchestrator.status === 'failed';

  return (
    <div
      className={`
        relative rounded-xl overflow-hidden shadow-card border
        transition-all duration-200
        ${
          isActive
            ? 'border-sc-purple/40 bg-gradient-to-br from-sc-purple/10 via-sc-bg-base to-sc-bg-base shadow-[0_0_20px_rgba(225,53,255,0.1)]'
            : isHumanReview
              ? 'border-sc-coral/40 bg-gradient-to-br from-sc-coral/10 via-sc-bg-base to-sc-bg-base shadow-[0_0_20px_rgba(255,106,193,0.1)]'
              : isComplete
                ? 'border-sc-green/30 bg-sc-bg-base'
                : isFailed
                  ? 'border-sc-red/30 bg-gradient-to-br from-sc-red/5 via-sc-bg-base to-sc-bg-base'
                  : 'border-sc-fg-subtle/20 bg-sc-bg-base'
        }
      `}
    >
      {/* Header */}
      <div className="p-4">
        <div className="flex items-start justify-between gap-3">
          {/* Left: Expand + Info */}
          <div className="flex items-start gap-2 min-w-0 flex-1">
            <button
              type="button"
              onClick={() => setIsExpanded(!isExpanded)}
              className="p-1 text-sc-fg-subtle hover:text-sc-purple transition-colors mt-0.5"
            >
              {isExpanded ? (
                <ChevronDown width={14} height={14} />
              ) : (
                <ChevronRight width={14} height={14} />
              )}
            </button>

            <div className="min-w-0 flex-1">
              {/* Phase + Status badges */}
              <div className="flex items-center gap-2 mb-1">
                <span
                  className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${phaseConfig.bgClass} ${phaseConfig.textClass}`}
                >
                  {phaseConfig.icon} {phaseConfig.label}
                  {orchestrator.rework_count > 0 && ` #${orchestrator.rework_count}`}
                </span>
                {isPaused && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-yellow/20 text-sc-yellow">
                    Paused
                  </span>
                )}
                {isComplete && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-green/20 text-sc-green">
                    Complete
                  </span>
                )}
                {isFailed && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-sc-red/20 text-sc-red">
                    Failed
                  </span>
                )}
              </div>

              {/* Task name */}
              <h3 className="text-sm font-medium text-sc-fg-primary line-clamp-1">
                {taskName ?? `Task ${orchestrator.task_id.slice(0, 8)}...`}
              </h3>

              {/* Phase pipeline */}
              <div className="mt-2">
                <PhasePipeline currentPhase={orchestrator.current_phase} />
              </div>
            </div>
          </div>

          {/* Right: Actions */}
          <div className="flex items-center gap-1">
            {isActive && (
              <button
                type="button"
                onClick={() => onPause(orchestrator.id)}
                className="p-1.5 text-sc-fg-subtle hover:text-sc-yellow hover:bg-sc-yellow/10 rounded-md transition-colors"
                title="Pause"
              >
                <Pause width={14} height={14} />
              </button>
            )}
            {isPaused && (
              <button
                type="button"
                onClick={() => onResume(orchestrator.id)}
                className="p-1.5 text-sc-fg-subtle hover:text-sc-green hover:bg-sc-green/10 rounded-md transition-colors"
                title="Resume"
              >
                <Play width={14} height={14} />
              </button>
            )}
            {isHumanReview && onApprove && onReject && (
              <>
                <button
                  type="button"
                  onClick={() => onApprove(orchestrator.id)}
                  className="flex items-center gap-1 px-2 py-1 text-xs font-medium text-sc-green hover:bg-sc-green/10 rounded-md transition-colors"
                  title="Approve"
                >
                  <CheckCircle width={14} height={14} />
                  <span>Approve</span>
                </button>
                <button
                  type="button"
                  onClick={() => onReject(orchestrator.id)}
                  className="flex items-center gap-1 px-2 py-1 text-xs font-medium text-sc-red hover:bg-sc-red/10 rounded-md transition-colors"
                  title="Reject"
                >
                  <XmarkCircle width={14} height={14} />
                  <span>Rework</span>
                </button>
              </>
            )}
          </div>
        </div>

        {/* Quality Gates */}
        {orchestrator.gate_config.length > 0 && (
          <div className="mt-3 pt-3 border-t border-sc-fg-subtle/10">
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-sc-fg-subtle font-medium">Gates:</span>
              <GatePipeline
                gates={orchestrator.gate_config}
                currentPhase={orchestrator.current_phase}
              />
            </div>
          </div>
        )}
      </div>

      {/* Worker section (collapsible) */}
      {isExpanded && worker && (
        <div className="border-t border-sc-fg-subtle/10 bg-sc-bg-elevated/30">
          <div className="px-4 py-2">
            <span className="text-[10px] text-sc-fg-subtle font-medium uppercase tracking-wide">
              Worker Agent
            </span>
          </div>
          <WorkerCard agent={worker} />
        </div>
      )}

      {/* No worker state */}
      {isExpanded && !worker && !isComplete && !isFailed && (
        <div className="border-t border-sc-fg-subtle/10 bg-sc-bg-elevated/30 p-4">
          <span className="text-xs text-sc-fg-muted">Spawning worker...</span>
        </div>
      )}
    </div>
  );
});
