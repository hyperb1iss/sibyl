'use client';

import { type FormEvent, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { EntityBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { FormField } from '@/components/ui/form-field';
import {
  Check,
  Eye,
  FileText,
  Flash,
  type IconComponent,
  LightBulb,
  Send,
} from '@/components/ui/icons';
import { Input, Textarea } from '@/components/ui/input';
import { Markdown } from '@/components/ui/markdown';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type {
  MemoryScope,
  SynthesisDepth,
  SynthesisDraftResponse,
  SynthesisOutputType,
  SynthesisPlanResponse,
  SynthesisSectionRequest,
} from '@/lib/api';
import { useSynthesisDraft, useSynthesisPlan } from '@/lib/hooks';
import { SynthesisOutlineEditor } from './synthesis-outline-editor';
import { SynthesisVerificationPanel } from './synthesis-verification-panel';

const OUTPUT_TYPES: Array<{ value: SynthesisOutputType; label: string }> = [
  { value: 'documentation', label: 'Documentation' },
  { value: 'report', label: 'Report' },
  { value: 'briefing', label: 'Briefing' },
  { value: 'roadmap', label: 'Roadmap' },
  { value: 'release_notes', label: 'Release Notes' },
  { value: 'audit_packet', label: 'Audit Packet' },
  { value: 'custom', label: 'Custom' },
];

const DEPTHS: Array<{ value: SynthesisDepth; label: string }> = [
  { value: 'brief', label: 'Brief' },
  { value: 'standard', label: 'Standard' },
  { value: 'deep', label: 'Deep' },
];

const REMEMBER_SCOPES: Array<{ value: MemoryScope; label: string }> = [
  { value: 'private', label: 'Private' },
  { value: 'delegated', label: 'Delegated' },
  { value: 'project', label: 'Project' },
];

interface SynthesisTemplate {
  id: string;
  label: string;
  emoji: string;
  description: string;
  goal: string;
  outputType: SynthesisOutputType;
  depth: SynthesisDepth;
  audience: string;
  seedQuery: string;
  constraints: string;
}

const TEMPLATES: SynthesisTemplate[] = [
  {
    id: 'architecture',
    label: 'Architecture Overview',
    emoji: '🏛️',
    description: 'Map the system. Components, contracts, evidence.',
    goal: 'Produce an architecture overview covering core components, data flow, contracts between services, and current known constraints. Cite source memories for every claim.',
    outputType: 'documentation',
    depth: 'deep',
    audience: 'engineering team',
    seedQuery: 'architecture overview system design',
    constraints:
      'include source ids per section\ncall out unresolved gaps\nprefer decisions over speculation',
  },
  {
    id: 'release',
    label: 'Release Notes',
    emoji: '🚀',
    description: 'Summarize what shipped. User-facing wins.',
    goal: 'Draft release notes for the current release. Highlight user-facing changes, breaking changes, and notable fixes. Cite source memories.',
    outputType: 'release_notes',
    depth: 'standard',
    audience: 'users',
    seedQuery: 'release notes user-facing changes',
    constraints: 'group by Added, Changed, Fixed\ninclude source ids per item',
  },
  {
    id: 'audit',
    label: 'Audit Packet',
    emoji: '🔍',
    description: 'Evidence trail with provenance and policy.',
    goal: 'Compile an audit packet covering policy decisions, evidence sources, recent corrections, and unresolved questions. Show full provenance.',
    outputType: 'audit_packet',
    depth: 'deep',
    audience: 'reviewers',
    seedQuery: 'audit evidence policy receipts',
    constraints: 'include policy reason codes\nlist freshness per source\ncall out any redactions',
  },
  {
    id: 'brief',
    label: 'Briefing',
    emoji: '⚡',
    description: 'Tight summary for catching up fast.',
    goal: 'Write a brief catching up someone returning to this work. Lead with current state, blockers, and the next decision needed.',
    outputType: 'briefing',
    depth: 'brief',
    audience: 'collaborator',
    seedQuery: 'recent decisions current state next steps',
    constraints: 'lead with the recommendation\nkeep it under 400 words',
  },
];

function textList(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map(part => part.trim())
    .filter(Boolean);
}

function outlineSections(plan: SynthesisPlanResponse): SynthesisSectionRequest[] {
  return plan.outline.sections.map(section => ({
    title: section.title,
    prompt: section.prompt,
    required_source_ids: [],
  }));
}

function sourceCount(plan: SynthesisPlanResponse | SynthesisDraftResponse | null): number {
  return plan ? new Set(plan.source_packs.flatMap(pack => pack.source_ids)).size : 0;
}

function StepDot({
  active,
  done,
  label,
  icon: Icon,
}: {
  active: boolean;
  done: boolean;
  label: string;
  icon: IconComponent;
}) {
  const dotClass = done
    ? 'bg-sc-green/20 border-sc-green/40 text-sc-green'
    : active
      ? 'bg-sc-purple/20 border-sc-purple/40 text-sc-purple shadow-[0_0_12px_color-mix(in_oklch,var(--sc-purple)_40%,transparent)]'
      : 'bg-sc-bg-highlight border-sc-fg-subtle/20 text-sc-fg-subtle';
  const textClass = done || active ? 'text-sc-fg-primary' : 'text-sc-fg-muted';
  return (
    <div className="flex items-center gap-2">
      <div
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold transition-all ${dotClass}`}
      >
        {done ? <Check width={12} height={12} /> : <Icon width={12} height={12} />}
      </div>
      <span className={`text-xs font-medium transition-colors ${textClass}`}>{label}</span>
    </div>
  );
}

function Stepper({
  hasPlan,
  hasDraft,
  hasRemembered,
}: {
  hasPlan: boolean;
  hasDraft: boolean;
  hasRemembered: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-base/60 px-4 py-2.5 shadow-card">
      <StepDot active={!hasPlan} done={hasPlan} label="1. Plan outline" icon={LightBulb} />
      <span className="h-px w-6 bg-sc-fg-subtle/20 sm:w-8" />
      <StepDot active={hasPlan && !hasDraft} done={hasDraft} label="2. Review & draft" icon={Eye} />
      <span className="h-px w-6 bg-sc-fg-subtle/20 sm:w-8" />
      <StepDot
        active={hasDraft && !hasRemembered}
        done={hasRemembered}
        label="3. Remember artifact"
        icon={Check}
      />
    </div>
  );
}

export function SynthesisRunner() {
  const planMutation = useSynthesisPlan();
  const draftMutation = useSynthesisDraft();

  const [goal, setGoal] = useState('');
  const [outputType, setOutputType] = useState<SynthesisOutputType>('documentation');
  const [depth, setDepth] = useState<SynthesisDepth>('standard');
  const [audience, setAudience] = useState('');
  const [seedQuery, setSeedQuery] = useState('');
  const [project, setProject] = useState('');
  const [domain, setDomain] = useState('');
  const [constraints, setConstraints] = useState('');
  const [maxSections, setMaxSections] = useState('5');
  const [remember, setRemember] = useState(false);
  const [rememberScope, setRememberScope] = useState<MemoryScope>('private');
  const [scopeKey, setScopeKey] = useState('');
  const [tags, setTags] = useState('synthesis');
  const [sections, setSections] = useState<SynthesisSectionRequest[]>([]);
  const [plan, setPlan] = useState<SynthesisPlanResponse | null>(null);
  const [draft, setDraft] = useState<SynthesisDraftResponse | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [appliedTemplate, setAppliedTemplate] = useState<string | null>(null);

  const activeRun = draft ?? plan;
  const totalSources = sourceCount(activeRun);
  const canDraft = sections.length > 0 && Boolean(goal.trim());
  const hasPlan = plan !== null;
  const hasDraft = draft !== null;
  const hasRemembered = Boolean(draft?.artifact.remembered_memory_id);

  function applyTemplate(template: SynthesisTemplate) {
    setGoal(template.goal);
    setOutputType(template.outputType);
    setDepth(template.depth);
    setAudience(template.audience);
    setSeedQuery(template.seedQuery);
    setConstraints(template.constraints);
    setAppliedTemplate(template.id);
  }

  const baseRequest = useMemo(
    () => ({
      goal: goal.trim(),
      output_type: outputType,
      audience: audience.trim() || null,
      depth,
      seed_query: seedQuery.trim() || null,
      project: project.trim() || null,
      domain: domain.trim() || null,
      constraints: textList(constraints),
      max_sections: Number(maxSections) || undefined,
      include_neighborhoods: true,
    }),
    [audience, constraints, depth, domain, goal, maxSections, outputType, project, seedQuery]
  );

  async function handlePlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!goal.trim()) {
      toast.error('Synthesis goal is required');
      return;
    }

    try {
      const response = await planMutation.mutateAsync(baseRequest);
      setPlan(response);
      setDraft(null);
      setSections(outlineSections(response));
      toast.success('Synthesis plan ready');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to plan synthesis');
    }
  }

  async function handleDraft() {
    if (!canDraft) return;

    try {
      const response = await draftMutation.mutateAsync({
        ...baseRequest,
        required_sections: sections,
        output_format: 'markdown',
        remember,
        ...(remember
          ? {
              memory_scope: rememberScope,
              scope_key: scopeKey.trim() || null,
              tags: textList(tags),
            }
          : {}),
      });
      setDraft(response);
      setPlan(response);
      toast.success(response.artifact.remembered_memory_id ? 'Draft remembered' : 'Draft ready');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Failed to draft synthesis');
    }
  }

  return (
    <div className="space-y-4">
      {/* Hero / intro band */}
      <div className="relative overflow-hidden rounded-xl border border-sc-purple/25 bg-gradient-to-br from-sc-bg-base via-sc-bg-elevated to-sc-purple/8 p-5 shadow-xl shadow-black/10">
        <div className="pointer-events-none absolute -top-20 -right-20 h-64 w-64 rounded-full bg-sc-purple/18 blur-3xl" />
        <div className="pointer-events-none absolute -bottom-16 -left-12 h-48 w-48 rounded-full bg-sc-cyan/12 blur-3xl" />

        <div className="relative flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-start gap-3 min-w-0">
            <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral shadow-lg shadow-sc-purple/30">
              <Flash width={20} height={20} className="text-white" />
            </div>
            <div className="min-w-0">
              <h1 className="text-xl sm:text-2xl font-bold text-sc-fg-primary">Memory Synthesis</h1>
              <p className="mt-1.5 max-w-2xl text-sm text-sc-fg-muted">
                Turn authorized memories into a source-grounded artifact. Plan an outline, inspect
                what each section will cite, draft Markdown, and optionally remember the result.
              </p>
            </div>
          </div>

          <Stepper hasPlan={hasPlan} hasDraft={hasDraft} hasRemembered={hasRemembered} />
        </div>
      </div>

      {/* Templates */}
      {!hasPlan && (
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-sc-fg-subtle">
            <Flash width={12} height={12} className="text-sc-purple" />
            <span>Quick start templates</span>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {TEMPLATES.map(template => {
              const isApplied = appliedTemplate === template.id;
              return (
                <button
                  key={template.id}
                  type="button"
                  onClick={() => applyTemplate(template)}
                  className={`group relative overflow-hidden rounded-xl border p-3 text-left transition-all shadow-card hover:shadow-card-hover ${
                    isApplied
                      ? 'border-sc-purple/50 bg-sc-purple/10'
                      : 'border-sc-fg-subtle/20 bg-sc-bg-base hover:border-sc-purple/40 hover:bg-sc-bg-highlight/50'
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <span className="text-lg leading-none">{template.emoji}</span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <p className="truncate text-sm font-semibold text-sc-fg-primary">
                          {template.label}
                        </p>
                        {isApplied && (
                          <Check width={12} height={12} className="shrink-0 text-sc-green" />
                        )}
                      </div>
                      <p className="mt-1 text-[11px] text-sc-fg-muted line-clamp-2">
                        {template.description}
                      </p>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(320px,440px)_minmax(0,1fr)]">
        {/* Left form column */}
        <section className="space-y-4 rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-sm font-semibold text-sc-fg-primary">Synthesis Setup</h2>
              <p className="mt-0.5 text-xs text-sc-fg-muted">
                Start with a goal. Plan, then draft.
              </p>
            </div>
            {appliedTemplate && (
              <button
                type="button"
                onClick={() => setAppliedTemplate(null)}
                className="text-[11px] text-sc-fg-subtle hover:text-sc-fg-primary transition-colors"
              >
                Clear template
              </button>
            )}
          </div>

          <form className="space-y-4" onSubmit={handlePlan}>
            <FormField label="Goal" required>
              {field => (
                <Textarea
                  {...field}
                  value={goal}
                  rows={4}
                  onChange={event => setGoal(event.target.value)}
                  placeholder="Summarize the v0.9 roadmap with remaining risks and evidence."
                />
              )}
            </FormField>

            <div className="grid gap-3 sm:grid-cols-2">
              <FormField label="Output Type">
                {field => (
                  <Select
                    value={outputType}
                    onValueChange={value => setOutputType(value as SynthesisOutputType)}
                  >
                    <SelectTrigger id={field.id} aria-describedby={field['aria-describedby']}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {OUTPUT_TYPES.map(type => (
                        <SelectItem key={type.value} value={type.value}>
                          {type.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </FormField>

              <FormField label="Depth">
                {field => (
                  <Select value={depth} onValueChange={value => setDepth(value as SynthesisDepth)}>
                    <SelectTrigger id={field.id} aria-describedby={field['aria-describedby']}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {DEPTHS.map(item => (
                        <SelectItem key={item.value} value={item.value}>
                          {item.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </FormField>
            </div>

            <FormField label="Audience">
              {field => (
                <Input
                  {...field}
                  value={audience}
                  onChange={event => setAudience(event.target.value)}
                  placeholder="maintainers"
                />
              )}
            </FormField>

            {/* Advanced */}
            <div className="rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-highlight/30">
              <button
                type="button"
                onClick={() => setAdvancedOpen(open => !open)}
                className="flex w-full items-center justify-between px-3 py-2 text-left"
                aria-expanded={advancedOpen}
              >
                <span className="text-xs font-medium uppercase tracking-wider text-sc-fg-subtle">
                  Advanced
                </span>
                <span className="text-[11px] text-sc-fg-muted">
                  {advancedOpen ? 'Hide' : 'Show'} retrieval & constraints
                </span>
              </button>
              {advancedOpen && (
                <div className="space-y-4 border-t border-sc-fg-subtle/10 px-3 py-3">
                  <FormField label="Seed Query">
                    {field => (
                      <Input
                        {...field}
                        value={seedQuery}
                        onChange={event => setSeedQuery(event.target.value)}
                        placeholder="roadmap synthesis memory workspace"
                      />
                    )}
                  </FormField>

                  <div className="grid gap-3 sm:grid-cols-2">
                    <FormField label="Project">
                      {field => (
                        <Input
                          {...field}
                          value={project}
                          onChange={event => setProject(event.target.value)}
                          placeholder="sibyl"
                        />
                      )}
                    </FormField>

                    <FormField label="Domain">
                      {field => (
                        <Input
                          {...field}
                          value={domain}
                          onChange={event => setDomain(event.target.value)}
                          placeholder="memory-runtime"
                        />
                      )}
                    </FormField>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_120px]">
                    <FormField label="Constraints">
                      {field => (
                        <Textarea
                          {...field}
                          value={constraints}
                          rows={3}
                          onChange={event => setConstraints(event.target.value)}
                          placeholder="include source ids, call out gaps"
                        />
                      )}
                    </FormField>

                    <FormField label="Sections">
                      {field => (
                        <Input
                          {...field}
                          type="number"
                          min={1}
                          max={12}
                          value={maxSections}
                          onChange={event => setMaxSections(event.target.value)}
                        />
                      )}
                    </FormField>
                  </div>
                </div>
              )}
            </div>

            <div className="space-y-3 rounded-lg border border-sc-coral/20 bg-sc-coral/5 p-3">
              <Checkbox
                checked={remember}
                onCheckedChange={checked => setRemember(checked === true)}
                label="Remember draft"
                description="Store the generated artifact as scoped memory with provenance."
              />

              {remember && (
                <>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <FormField label="Remember Scope">
                      {field => (
                        <Select
                          value={rememberScope}
                          onValueChange={value => setRememberScope(value as MemoryScope)}
                          disabled={!remember}
                        >
                          <SelectTrigger id={field.id} aria-describedby={field['aria-describedby']}>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {REMEMBER_SCOPES.map(scope => (
                              <SelectItem key={scope.value} value={scope.value}>
                                {scope.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      )}
                    </FormField>

                    <FormField label="Scope Key">
                      {field => (
                        <Input
                          {...field}
                          value={scopeKey}
                          disabled={!remember}
                          onChange={event => setScopeKey(event.target.value)}
                          placeholder="project id"
                        />
                      )}
                    </FormField>
                  </div>

                  <FormField label="Tags">
                    {field => (
                      <Input
                        {...field}
                        value={tags}
                        disabled={!remember}
                        onChange={event => setTags(event.target.value)}
                        placeholder="comma-separated tags"
                      />
                    )}
                  </FormField>

                  <div className="rounded border border-sc-yellow/20 bg-sc-yellow/10 px-3 py-2 text-[11px] text-sc-yellow">
                    Broad share scopes stay preview-only in this surface.
                  </div>
                </>
              )}
            </div>

            <div className="flex flex-wrap gap-2 pt-1">
              <Button
                type="submit"
                loading={planMutation.isPending}
                icon={<LightBulb width={16} />}
              >
                {hasPlan ? 'Re-plan' : 'Plan'}
              </Button>
              <Button
                type="button"
                variant="secondary"
                loading={draftMutation.isPending}
                disabled={!canDraft}
                icon={<Send width={16} />}
                onClick={handleDraft}
              >
                {hasDraft ? 'Re-draft' : 'Draft'}
              </Button>
            </div>
          </form>
        </section>

        {/* Right results column */}
        <div className="space-y-4">
          {activeRun ? (
            <div className="grid gap-3 md:grid-cols-3">
              <StatusCard
                label="Run"
                value={activeRun.run_id}
                tone="purple"
                icon={Flash}
                truncate
              />
              <StatusCard
                label="Sources"
                value={String(totalSources)}
                tone="cyan"
                icon={FileText}
              />
              <StatusCard
                label="Status"
                value={activeRun.status}
                tone={activeRun.status === 'verified' ? 'green' : 'yellow'}
                icon={Check}
              />
            </div>
          ) : (
            <EmptyResultState />
          )}

          {hasPlan && (
            <SynthesisOutlineEditor
              sections={sections}
              sourcePacks={activeRun?.source_packs ?? []}
              onSectionsChange={setSections}
            />
          )}

          {activeRun?.verification && (
            <SynthesisVerificationPanel verification={activeRun.verification} />
          )}

          {draft?.artifact && (
            <section className="space-y-4 rounded-xl border border-sc-green/25 bg-gradient-to-br from-sc-green/5 to-sc-bg-base p-5 shadow-card">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <FileText width={16} height={16} className="text-sc-green shrink-0" />
                    <h2 className="text-sm font-semibold text-sc-fg-primary truncate">
                      {draft.artifact.title}
                    </h2>
                  </div>
                  <p className="mt-1 truncate font-mono text-[11px] text-sc-fg-subtle">
                    {draft.artifact.artifact_id} · {draft.artifact.generated_text_hash}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {draft.artifact.remembered_memory_id && (
                    <EntityBadge type="remembered" className="border-sc-green/40 text-sc-green" />
                  )}
                  {draft.artifact.remembered_source_id && <EntityBadge type="source" />}
                </div>
              </div>
              <div className="rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-base/60 p-4">
                <Markdown content={draft.artifact.markdown} />
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

function StatusCard({
  label,
  value,
  tone,
  icon: Icon,
  truncate = false,
}: {
  label: string;
  value: string;
  tone: 'purple' | 'cyan' | 'green' | 'yellow';
  icon: IconComponent;
  truncate?: boolean;
}) {
  const toneClasses = {
    purple: 'border-sc-purple/25 bg-sc-purple/5 text-sc-purple',
    cyan: 'border-sc-cyan/25 bg-sc-cyan/5 text-sc-cyan',
    green: 'border-sc-green/25 bg-sc-green/5 text-sc-green',
    yellow: 'border-sc-yellow/25 bg-sc-yellow/5 text-sc-yellow',
  }[tone];

  return (
    <div className={`rounded-xl border bg-sc-bg-base p-4 shadow-card ${toneClasses}`}>
      <div className="flex items-center justify-between">
        <p className="text-[10px] font-medium uppercase tracking-wider text-sc-fg-subtle">
          {label}
        </p>
        <Icon width={14} height={14} />
      </div>
      <p className={`mt-2 text-sm font-semibold text-sc-fg-primary ${truncate ? 'truncate' : ''}`}>
        {value}
      </p>
    </div>
  );
}

function EmptyResultState() {
  return (
    <div className="rounded-xl border border-dashed border-sc-fg-subtle/25 bg-sc-bg-base/40 p-8 text-center shadow-card">
      <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-sc-purple/15 to-sc-cyan/10 shadow-inner">
        <Flash width={20} height={20} className="text-sc-purple" />
      </div>
      <h3 className="mt-3 text-sm font-semibold text-sc-fg-primary">Pick a goal, then plan</h3>
      <p className="mx-auto mt-1 max-w-sm text-xs text-sc-fg-muted">
        Sibyl will propose an outline from authorized memory and show you which sources back each
        section before drafting a thing.
      </p>
      <div className="mt-4 grid gap-2 text-left text-[11px] text-sc-fg-muted sm:grid-cols-3">
        <Hint icon={LightBulb} tone="purple" text="Plan reads the graph, never drafts text" />
        <Hint icon={Eye} tone="cyan" text="Review sources before drafting" />
        <Hint icon={Check} tone="green" text="Remember to keep provenance" />
      </div>
    </div>
  );
}

function Hint({
  icon: Icon,
  tone,
  text,
}: {
  icon: IconComponent;
  tone: 'purple' | 'cyan' | 'green';
  text: string;
}) {
  const toneColor = {
    purple: 'text-sc-purple',
    cyan: 'text-sc-cyan',
    green: 'text-sc-green',
  }[tone];
  return (
    <div className="flex items-start gap-2 rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-base/60 p-2.5">
      <Icon width={12} height={12} className={`${toneColor} mt-0.5 shrink-0`} />
      <span>{text}</span>
    </div>
  );
}
