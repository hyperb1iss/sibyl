'use client';

import Link from 'next/link';
import { FormField } from '@/components/ui/form-field';
import { Input, Textarea } from '@/components/ui/input';
import type { SynthesisSectionRequest, SynthesisSourcePack } from '@/lib/api/memory';

function sourceIdsToText(sourceIds: string[] | undefined): string {
  return (sourceIds ?? []).join(', ');
}

function textToSourceIds(value: string): string[] {
  return value
    .split(',')
    .map(part => part.trim())
    .filter(Boolean);
}

function shortId(value: string): string {
  if (value.length <= 24) return value;
  return `${value.slice(0, 10)}...${value.slice(-6)}`;
}

function sourceHref(sourceId: string): string {
  return `/memory/sources/${encodeURIComponent(sourceId)}`;
}

function impactLabels(sourcePack: SynthesisSourcePack): string[] {
  const labels: string[] = [];
  if (sourcePack.hidden_count > 0) labels.push(`${sourcePack.hidden_count} hidden`);
  if (sourcePack.redaction_count > 0) labels.push(`${sourcePack.redaction_count} redacted`);
  if (sourcePack.correction_count > 0) labels.push(`${sourcePack.correction_count} corrected`);
  return labels;
}

export function SynthesisOutlineEditor({
  sections,
  sourcePacks,
  onSectionsChange,
}: {
  sections: SynthesisSectionRequest[];
  sourcePacks: SynthesisSourcePack[];
  onSectionsChange: (sections: SynthesisSectionRequest[]) => void;
}) {
  function updateSection(index: number, patch: Partial<SynthesisSectionRequest>) {
    onSectionsChange(
      sections.map((section, i) => (i === index ? { ...section, ...patch } : section))
    );
  }

  return (
    <section className="space-y-3 rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-elevated p-4 shadow-card">
      <div>
        <h2 className="text-sm font-semibold text-sc-fg-primary">Outline Review</h2>
        <p className="mt-1 text-sm text-sc-fg-muted">
          Tune section prompts and required sources before drafting.
        </p>
      </div>

      {sections.length === 0 ? (
        <p className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/40 px-3 py-4 text-sm text-sc-fg-muted">
          Plan a synthesis run to review the outline.
        </p>
      ) : (
        <div className="space-y-3">
          {sections.map((section, index) => {
            const sourcePack = sourcePacks[index];
            const requiredSourceCount = section.required_source_ids?.length ?? 0;
            const sourceCount =
              requiredSourceCount > 0 ? requiredSourceCount : (sourcePack?.source_ids.length ?? 0);
            const impact = sourcePack ? impactLabels(sourcePack) : [];
            const freshnessEntries = sourcePack ? Object.entries(sourcePack.freshness) : [];
            const correctionReasonEntries = sourcePack
              ? Object.entries(sourcePack.correction_reasons)
              : [];

            return (
              <article
                key={`section-${index}`}
                className="space-y-3 rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/40 p-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">
                    Section {index + 1}
                  </p>
                  <span className="inline-flex items-center rounded-full bg-sc-bg-highlight px-2 py-0.5 text-xs font-medium text-sc-fg-muted">
                    {sourceCount} {sourceCount === 1 ? 'source' : 'sources'}
                  </span>
                </div>

                <FormField label="Title">
                  {field => (
                    <Input
                      {...field}
                      value={section.title}
                      onChange={event => updateSection(index, { title: event.target.value })}
                    />
                  )}
                </FormField>

                <FormField label="Prompt">
                  {field => (
                    <Textarea
                      {...field}
                      value={section.prompt ?? ''}
                      rows={3}
                      onChange={event => updateSection(index, { prompt: event.target.value })}
                    />
                  )}
                </FormField>

                <FormField label="Required Sources">
                  {field => (
                    <Input
                      {...field}
                      value={sourceIdsToText(section.required_source_ids)}
                      onChange={event =>
                        updateSection(index, {
                          required_source_ids: textToSourceIds(event.target.value),
                        })
                      }
                      placeholder="source ids separated by commas"
                    />
                  )}
                </FormField>

                {sourcePack && (
                  <div className="rounded-lg border border-sc-cyan/20 bg-sc-cyan/10 p-3">
                    <p className="text-xs font-medium uppercase tracking-[0.12em] text-sc-cyan">
                      Source Pack
                    </p>
                    <div className="mt-2 space-y-2">
                      {sourcePack.sources.slice(0, 4).map(source => (
                        <div
                          key={source.id}
                          className="rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight/40 px-3 py-2"
                        >
                          <Link
                            href={sourceHref(source.id)}
                            className="block truncate text-sm font-medium text-sc-fg-primary transition-colors hover:text-sc-purple"
                            title={source.id}
                          >
                            {source.name}
                          </Link>
                          <p className="mt-0.5 truncate font-mono text-[11px] text-sc-cyan">
                            {shortId(source.id)}
                          </p>
                          <p className="mt-1 line-clamp-2 text-xs text-sc-fg-muted">
                            {source.content_preview}
                          </p>
                        </div>
                      ))}
                    </div>
                    {sourcePack.source_ids.length > sourcePack.sources.length && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {sourcePack.source_ids.slice(sourcePack.sources.length, 10).map(id => (
                          <Link
                            key={id}
                            href={sourceHref(id)}
                            className="rounded border border-sc-cyan/20 bg-sc-cyan/10 px-1.5 py-0.5 font-mono text-[11px] text-sc-cyan transition-colors hover:border-sc-purple/40 hover:text-sc-purple"
                            title={id}
                          >
                            {shortId(id)}
                          </Link>
                        ))}
                      </div>
                    )}
                    {impact.length > 0 && (
                      <p className="mt-2 text-xs text-sc-yellow">{impact.join(' · ')}</p>
                    )}
                    {correctionReasonEntries.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {correctionReasonEntries.slice(0, 4).map(([reason, count]) => (
                          <span
                            key={reason}
                            className="rounded border border-sc-yellow/20 bg-sc-yellow/10 px-1.5 py-0.5 text-[11px] text-sc-yellow"
                          >
                            {reason.replace(/_/g, ' ')}
                            {count > 1 ? ` (${count})` : ''}
                          </span>
                        ))}
                      </div>
                    )}
                    {freshnessEntries.length > 0 && (
                      <dl className="mt-2 grid gap-1 text-[11px]">
                        {freshnessEntries.slice(0, 4).map(([id, timestamp]) => (
                          <div key={id} className="flex min-w-0 items-center justify-between gap-3">
                            <dt className="truncate font-mono text-sc-fg-subtle">{shortId(id)}</dt>
                            <dd className="truncate text-sc-fg-muted">{timestamp ?? 'unknown'}</dd>
                          </div>
                        ))}
                      </dl>
                    )}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
