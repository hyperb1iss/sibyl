'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Breadcrumb } from '@/components/layout/breadcrumb';
import { PageHeader } from '@/components/layout/page-header';
import { EntityBadge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { EnhancedEmptyState } from '@/components/ui/empty-state';
import {
  Archive,
  Calendar,
  Command,
  ExternalLink,
  Hash,
  LayoutDashboard,
  Search,
  User,
} from '@/components/ui/icons';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { LoadingState } from '@/components/ui/spinner';
import { ErrorState } from '@/components/ui/tooltip';
import { formatDateTime, formatDistanceToNow } from '@/lib/constants';
import { useRawCapture, useRawCaptures } from '@/lib/hooks';

const MAX_CAPTURE_RESULTS = 200;

function titleCase(value: string): string {
  return value
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function surfaceLabel(surface: string | null): string {
  if (!surface) {
    return 'Unknown';
  }

  if (surface === 'cli') {
    return 'CLI';
  }

  return titleCase(surface);
}

function SurfaceBadge({ surface }: { surface: string | null }) {
  const Icon = surface === 'dashboard' ? LayoutDashboard : surface === 'cli' ? Command : Archive;

  return (
    <span className="inline-flex items-center gap-1 rounded border border-sc-cyan/20 bg-sc-cyan/10 px-2 py-0.5 text-xs font-medium text-sc-cyan">
      <Icon width={12} height={12} />
      {surfaceLabel(surface)}
    </span>
  );
}

function archiveMeta(capturesCount: number, filteredCount: number, hasMore: boolean): string {
  const countLabel =
    filteredCount === capturesCount
      ? `${capturesCount} captures`
      : `${filteredCount} of ${capturesCount} captures`;

  return hasMore ? `${countLabel} | newest ${MAX_CAPTURE_RESULTS}` : countLabel;
}

export default function ArchivePage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [searchQuery, setSearchQuery] = useState('');
  const [surfaceFilter, setSurfaceFilter] = useState('all');
  const [typeFilter, setTypeFilter] = useState('all');
  const [linkFilter, setLinkFilter] = useState<'all' | 'linked' | 'unlinked'>('all');

  const { data, isLoading, error } = useRawCaptures({
    limit: MAX_CAPTURE_RESULTS,
    entity_type: typeFilter === 'all' ? undefined : typeFilter,
    capture_surface: surfaceFilter === 'all' ? undefined : surfaceFilter,
  });

  const captures = data?.captures ?? [];
  const surfaceOptions = useMemo(
    () =>
      Array.from(
        new Set(
          captures
            .map(capture => capture.capture_surface)
            .filter((surface): surface is string => Boolean(surface))
        )
      ).sort(),
    [captures]
  );
  const typeOptions = useMemo(
    () => Array.from(new Set(captures.map(capture => capture.entity_type))).sort(),
    [captures]
  );

  const filteredCaptures = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    return captures.filter(capture => {
      if (linkFilter === 'linked' && !capture.entity_id) {
        return false;
      }
      if (linkFilter === 'unlinked' && capture.entity_id) {
        return false;
      }
      if (!query) {
        return true;
      }

      const tags = capture.tags.join(' ').toLowerCase();
      const metadata = JSON.stringify(capture.metadata).toLowerCase();
      return (
        capture.title.toLowerCase().includes(query) ||
        capture.entity_type.toLowerCase().includes(query) ||
        capture.entity_id?.toLowerCase().includes(query) ||
        tags.includes(query) ||
        metadata.includes(query)
      );
    });
  }, [captures, linkFilter, searchQuery]);

  const requestedCaptureId = searchParams.get('id');
  const activeCaptureId = useMemo(() => {
    if (requestedCaptureId && filteredCaptures.some(capture => capture.id === requestedCaptureId)) {
      return requestedCaptureId;
    }

    return filteredCaptures[0]?.id ?? '';
  }, [filteredCaptures, requestedCaptureId]);

  const {
    data: selectedCapture,
    isLoading: isCaptureLoading,
    error: captureError,
  } = useRawCapture(activeCaptureId, {
    enabled: Boolean(activeCaptureId),
  });

  const updateSelection = useCallback(
    (captureId: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (captureId) {
        params.set('id', captureId);
      } else {
        params.delete('id');
      }
      const nextUrl = params.toString() ? `/archive?${params.toString()}` : '/archive';
      router.replace(nextUrl, { scroll: false });
    },
    [router, searchParams]
  );

  const clearFilters = useCallback(() => {
    setSearchQuery('');
    setSurfaceFilter('all');
    setTypeFilter('all');
    setLinkFilter('all');
  }, []);

  useEffect(() => {
    if (!requestedCaptureId || requestedCaptureId === activeCaptureId) {
      return;
    }

    updateSelection(activeCaptureId || null);
  }, [activeCaptureId, requestedCaptureId, updateSelection]);

  const stats = useMemo(() => {
    return {
      total: captures.length,
      surfaces: new Set(captures.map(capture => capture.capture_surface).filter(Boolean)).size,
      linked: captures.filter(capture => capture.entity_id).length,
      unlinked: captures.filter(capture => !capture.entity_id).length,
    };
  }, [captures]);

  if (error) {
    return (
      <div className="space-y-4">
        <Breadcrumb />
        <PageHeader description="Review verbatim quick captures before and after graph extraction" />
        <ErrorState
          title="Failed to load archive"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Breadcrumb />
        <PageHeader description="Review verbatim quick captures before and after graph extraction" />
        <LoadingState message="Loading archive..." />
      </div>
    );
  }

  if (captures.length === 0) {
    return (
      <div className="space-y-4">
        <Breadcrumb />
        <PageHeader description="Review verbatim quick captures before and after graph extraction" />
        <EnhancedEmptyState
          icon={<Archive width={40} height={40} className="text-sc-cyan" />}
          title="No raw captures yet"
          description="Quick captures will appear here verbatim so you can audit what Sibyl kept before graph compression."
          actions={[{ label: 'Browse Entities', href: '/entities', variant: 'secondary' }]}
        />
      </div>
    );
  }

  return (
    <div className="space-y-4 animate-fade-in">
      <Breadcrumb />

      <PageHeader
        description="Review verbatim quick captures before and after graph extraction"
        meta={archiveMeta(captures.length, filteredCaptures.length, Boolean(data?.has_more))}
      />

      {data?.has_more && (
        <div className="rounded-xl border border-sc-yellow/30 bg-sc-yellow/10 px-4 py-3 text-sm text-sc-yellow">
          Showing the newest {MAX_CAPTURE_RESULTS} captures right now. Older archive entries are
          still available through the API and CLI.
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-3">
        <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
          <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">Raw Captures</p>
          <p className="mt-2 text-2xl font-semibold text-sc-fg-primary">{stats.total}</p>
          <p className="mt-1 text-sm text-sc-fg-muted">Verbatim quick-capture snapshots</p>
        </div>
        <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
          <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">Needs Link</p>
          <p className="mt-2 text-2xl font-semibold text-sc-fg-primary">{stats.unlinked}</p>
          <p className="mt-1 text-sm text-sc-fg-muted">Captures that still need graph linkage</p>
        </div>
        <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
          <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">Linked Entities</p>
          <p className="mt-2 text-2xl font-semibold text-sc-fg-primary">{stats.linked}</p>
          <p className="mt-1 text-sm text-sc-fg-muted">Captures already attached to graph memory</p>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
        <section className="space-y-3 rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-2">
              {[
                { value: 'all', label: 'All', count: captures.length },
                { value: 'linked', label: 'Linked', count: stats.linked },
                { value: 'unlinked', label: 'Needs Link', count: stats.unlinked },
              ].map(option => {
                const active = linkFilter === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setLinkFilter(option.value as typeof linkFilter)}
                    className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${
                      active
                        ? 'border-sc-purple/30 bg-sc-purple/15 text-sc-purple'
                        : 'border-sc-fg-subtle/20 bg-sc-bg-highlight text-sc-fg-muted hover:border-sc-cyan/30 hover:text-sc-fg-primary'
                    }`}
                  >
                    <span>{option.label}</span>
                    <span className="rounded-full bg-black/20 px-1.5 py-0.5 text-[10px]">
                      {option.count}
                    </span>
                  </button>
                );
              })}
            </div>

            <Input
              type="text"
              value={searchQuery}
              onChange={event => setSearchQuery(event.target.value)}
              placeholder="Search titles, tags, metadata..."
              icon={<Search width={16} height={16} />}
            />

            <div className="grid gap-2 sm:grid-cols-2">
              <Select value={surfaceFilter} onValueChange={setSurfaceFilter}>
                <SelectTrigger>
                  <SelectValue placeholder="All surfaces" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All surfaces</SelectItem>
                  {surfaceOptions.map(surface => (
                    <SelectItem key={surface} value={surface}>
                      {surfaceLabel(surface)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>

              <Select value={typeFilter} onValueChange={setTypeFilter}>
                <SelectTrigger>
                  <SelectValue placeholder="All entity types" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All entity types</SelectItem>
                  {typeOptions.map(entityType => (
                    <SelectItem key={entityType} value={entityType}>
                      {titleCase(entityType)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {(searchQuery ||
              surfaceFilter !== 'all' ||
              typeFilter !== 'all' ||
              linkFilter !== 'all') && (
              <div className="flex justify-end">
                <Button variant="ghost" size="sm" onClick={clearFilters}>
                  Clear filters
                </Button>
              </div>
            )}
          </div>

          {filteredCaptures.length === 0 ? (
            <EnhancedEmptyState
              icon={<Archive width={40} height={40} className="text-sc-yellow" />}
              title="No captures match"
              description="Try a different search term or reset your filters to widen the archive."
              variant="filtered"
              actions={[{ label: 'Clear filters', onClick: clearFilters }]}
            />
          ) : (
            <div className="space-y-3">
              {filteredCaptures.map(capture => {
                const isActive = capture.id === activeCaptureId;
                return (
                  <button
                    key={capture.id}
                    type="button"
                    aria-label={`Select capture ${capture.title}`}
                    onClick={() => updateSelection(capture.id)}
                    className={`w-full rounded-2xl border p-4 text-left transition-all ${
                      isActive
                        ? 'border-sc-purple/40 bg-sc-purple/10 shadow-lg shadow-sc-purple/10'
                        : 'border-sc-fg-subtle/20 bg-sc-bg-base hover:border-sc-cyan/40 hover:bg-sc-bg-highlight/50'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h2 className="truncate text-sm font-semibold text-sc-fg-primary">
                          {capture.title}
                        </h2>
                        <p className="mt-1 text-xs text-sc-fg-muted">
                          {formatDistanceToNow(capture.created_at)}
                        </p>
                      </div>
                      <SurfaceBadge surface={capture.capture_surface} />
                    </div>

                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <EntityBadge type={capture.entity_type} />
                      {!capture.entity_id && (
                        <span className="rounded border border-sc-yellow/30 bg-sc-yellow/10 px-2 py-0.5 text-xs font-medium text-sc-yellow">
                          Needs link
                        </span>
                      )}
                      {capture.tags.slice(0, 3).map(tag => (
                        <span
                          key={`${capture.id}-${tag}`}
                          className="rounded border border-sc-fg-subtle/20 bg-sc-bg-highlight px-2 py-0.5 text-xs text-sc-fg-muted"
                        >
                          #{tag}
                        </span>
                      ))}
                    </div>

                    {capture.entity_id && (
                      <div className="mt-3 inline-flex items-center gap-1.5 text-xs text-sc-fg-subtle">
                        <Hash width={12} height={12} />
                        <span className="truncate">{capture.entity_id}</span>
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </section>

        <section className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4 shadow-card">
          {!activeCaptureId ? (
            <EnhancedEmptyState
              icon={<Archive width={40} height={40} className="text-sc-fg-subtle" />}
              title="Pick a capture"
              description="Select a capture from the archive to inspect the exact saved content."
            />
          ) : isCaptureLoading ? (
            <LoadingState message="Loading raw capture..." />
          ) : captureError ? (
            <ErrorState
              title="Failed to load capture"
              message={captureError instanceof Error ? captureError.message : 'Unknown error'}
            />
          ) : !selectedCapture ? (
            <EnhancedEmptyState
              icon={<Archive width={40} height={40} className="text-sc-fg-subtle" />}
              title="Capture not found"
              description="This archive entry may have been removed or is outside the current filter view."
            />
          ) : (
            <div className="space-y-4">
              <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-highlight/40 p-5">
                <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                  <div className="min-w-0">
                    <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">
                      Archive Entry
                    </p>
                    <h1 className="mt-2 text-2xl font-semibold text-sc-fg-primary">
                      {selectedCapture.title}
                    </h1>
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <EntityBadge type={selectedCapture.entity_type} size="md" showIcon />
                      <SurfaceBadge surface={selectedCapture.capture_surface} />
                      {selectedCapture.tags.map(tag => (
                        <span
                          key={`${selectedCapture.id}-${tag}`}
                          className="rounded border border-sc-fg-subtle/20 bg-sc-bg-base px-2 py-0.5 text-xs text-sc-fg-muted"
                        >
                          #{tag}
                        </span>
                      ))}
                    </div>
                  </div>

                  <div className="rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-base px-4 py-3 text-sm text-sc-fg-muted">
                    <div className="flex items-center gap-2">
                      <Calendar width={14} height={14} className="text-sc-cyan" />
                      <span>{formatDateTime(selectedCapture.created_at)}</span>
                    </div>
                  </div>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-2">
                <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">
                    Capture Surface
                  </p>
                  <div className="mt-3">
                    <SurfaceBadge surface={selectedCapture.capture_surface} />
                  </div>
                </div>

                <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">
                    Linked Entity
                  </p>
                  <div className="mt-3 text-sm text-sc-fg-primary">
                    {selectedCapture.entity_id ? (
                      <Link
                        href={`/entities/${selectedCapture.entity_id}`}
                        className="inline-flex items-center gap-2 text-sc-cyan transition-colors hover:text-sc-purple"
                      >
                        <ExternalLink width={14} height={14} />
                        {selectedCapture.entity_id}
                      </Link>
                    ) : (
                      <span className="text-sc-fg-muted">Not linked yet</span>
                    )}
                  </div>
                </div>

                <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">
                    Created By
                  </p>
                  <div className="mt-3 inline-flex items-center gap-2 text-sm text-sc-fg-primary">
                    <User width={14} height={14} className="text-sc-cyan" />
                    <span>{selectedCapture.created_by_user_id ?? 'System or unknown user'}</span>
                  </div>
                </div>

                <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4">
                  <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">
                    Metadata Keys
                  </p>
                  <p className="mt-3 text-sm text-sc-fg-primary">
                    {Object.keys(selectedCapture.metadata).length}
                  </p>
                </div>
              </div>

              <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4">
                <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">
                  Verbatim Content
                </p>
                <pre className="mt-4 overflow-x-auto whitespace-pre-wrap break-words rounded-xl border border-sc-fg-subtle/20 bg-black/20 p-4 font-mono text-sm leading-6 text-sc-fg-primary">
                  {selectedCapture.raw_content || '(empty capture)'}
                </pre>
              </div>

              <div className="rounded-2xl border border-sc-fg-subtle/20 bg-sc-bg-base p-4">
                <p className="text-xs uppercase tracking-[0.12em] text-sc-fg-subtle">
                  Original Metadata
                </p>
                <pre className="mt-4 overflow-x-auto rounded-xl border border-sc-fg-subtle/20 bg-sc-bg-highlight p-4 font-mono text-xs leading-6 text-sc-fg-muted">
                  {JSON.stringify(selectedCapture.metadata, null, 2)}
                </pre>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
