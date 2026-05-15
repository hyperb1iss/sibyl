'use client';

import { type FormEvent, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { EntityBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { FormField } from '@/components/ui/form-field';
import { FileText, Send } from '@/components/ui/icons';
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

  const activeRun = draft ?? plan;
  const totalSources = sourceCount(activeRun);
  const canDraft = sections.length > 0 && Boolean(goal.trim());

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
    <div className="grid gap-4 xl:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
      <section className="space-y-4 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
        <div>
          <h2 className="text-sm font-semibold text-sc-fg-primary">Synthesis Setup</h2>
          <p className="mt-1 text-sm text-sc-fg-muted">
            Build a source-backed artifact from scoped Sibyl memory.
          </p>
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

          <div className="space-y-3 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/40 p-3">
            <Checkbox
              checked={remember}
              onCheckedChange={checked => setRemember(checked === true)}
              label="Remember draft"
              description="Store the generated artifact as scoped memory."
            />

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

            <div className="rounded border border-sc-yellow/20 bg-sc-yellow/10 px-3 py-2 text-xs text-sc-yellow">
              Broad share scopes stay preview-only in this surface.
            </div>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button type="submit" loading={planMutation.isPending} icon={<FileText width={16} />}>
              Plan
            </Button>
            <Button
              type="button"
              variant="secondary"
              loading={draftMutation.isPending}
              disabled={!canDraft}
              icon={<Send width={16} />}
              onClick={handleDraft}
            >
              Draft
            </Button>
          </div>
        </form>
      </section>

      <div className="space-y-4">
        <div className="grid gap-3 md:grid-cols-3">
          <div className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
            <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">Run</p>
            <p className="mt-2 truncate text-sm font-medium text-sc-fg-primary">
              {activeRun?.run_id ?? 'not planned'}
            </p>
          </div>
          <div className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
            <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">Sources</p>
            <p className="mt-2 text-xl font-semibold text-sc-fg-primary">{totalSources}</p>
          </div>
          <div className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
            <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">Status</p>
            <p className="mt-2 text-sm font-medium text-sc-fg-primary">
              {activeRun?.status ?? 'idle'}
            </p>
          </div>
        </div>

        <SynthesisOutlineEditor
          sections={sections}
          sourcePacks={activeRun?.source_packs ?? []}
          onSectionsChange={setSections}
        />

        {activeRun?.verification && (
          <SynthesisVerificationPanel verification={activeRun.verification} />
        )}

        {draft?.artifact && (
          <section className="space-y-4 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-sc-fg-primary">{draft.artifact.title}</h2>
                <p className="mt-1 text-xs text-sc-fg-subtle">
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
            <Markdown content={draft.artifact.markdown} />
          </section>
        )}
      </div>
    </div>
  );
}
