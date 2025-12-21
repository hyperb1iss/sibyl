'use client';

import { useState } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { LoadingState } from '@/components/ui/spinner';
import { EmptyState, ErrorState } from '@/components/ui/tooltip';
import { SourceCard, SourceCardSkeleton } from '@/components/sources';
import { useSources, useCreateSource, useDeleteSource, useCrawlSource } from '@/lib/hooks';
import { Input } from '@/components/ui/input';

export default function SourcesPage() {
  const { data: sourcesData, isLoading, error } = useSources();
  const createSource = useCreateSource();
  const deleteSource = useDeleteSource();
  const crawlSource = useCrawlSource();

  const [showAddForm, setShowAddForm] = useState(false);
  const [newSourceUrl, setNewSourceUrl] = useState('');
  const [newSourceName, setNewSourceName] = useState('');

  const sources = sourcesData?.entities ?? [];

  const handleAddSource = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newSourceUrl.trim()) return;

    try {
      // Auto-generate name from URL if not provided
      const name = newSourceName.trim() || new URL(newSourceUrl).hostname;

      await createSource.mutateAsync({
        name,
        url: newSourceUrl.trim(),
        description: '',
        source_type: 'website',
        crawl_depth: 2,
      });

      setNewSourceUrl('');
      setNewSourceName('');
      setShowAddForm(false);
    } catch (err) {
      console.error('Failed to create source:', err);
    }
  };

  const handleCrawl = async (id: string) => {
    try {
      await crawlSource.mutateAsync(id);
    } catch (err) {
      console.error('Failed to trigger crawl:', err);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Are you sure you want to delete this source and all its documents?')) {
      return;
    }
    try {
      await deleteSource.mutateAsync(id);
    } catch (err) {
      console.error('Failed to delete source:', err);
    }
  };

  if (error) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Knowledge Sources"
          description="Manage documentation sources for the knowledge graph"
        />
        <ErrorState
          title="Failed to load sources"
          message={error instanceof Error ? error.message : 'Unknown error'}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Knowledge Sources"
        description="Manage documentation sources for the knowledge graph"
        meta={`${sources.length} sources`}
        action={
          !showAddForm && (
            <Button onClick={() => setShowAddForm(true)}>
              + Add Source
            </Button>
          )
        }
      />

      {/* Add Source Form */}
      {showAddForm && (
        <div className="bg-sc-bg-base border border-sc-purple/30 rounded-xl p-6">
          <h3 className="text-lg font-semibold text-sc-fg-primary mb-4">Add New Source</h3>
          <form onSubmit={handleAddSource} className="space-y-4">
            <div>
              <label htmlFor="url" className="block text-sm font-medium text-sc-fg-muted mb-2">
                Source URL *
              </label>
              <Input
                id="url"
                type="url"
                value={newSourceUrl}
                onChange={(e) => setNewSourceUrl(e.target.value)}
                placeholder="https://docs.example.com"
                required
              />
            </div>
            <div>
              <label htmlFor="name" className="block text-sm font-medium text-sc-fg-muted mb-2">
                Name (optional)
              </label>
              <Input
                id="name"
                type="text"
                value={newSourceName}
                onChange={(e) => setNewSourceName(e.target.value)}
                placeholder="Auto-generated from URL if empty"
              />
            </div>
            <div className="flex gap-3 justify-end">
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setShowAddForm(false);
                  setNewSourceUrl('');
                  setNewSourceName('');
                }}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={!newSourceUrl.trim() || createSource.isPending}
              >
                {createSource.isPending ? 'Adding...' : 'Add Source'}
              </Button>
            </div>
          </form>
        </div>
      )}

      {/* Sources Grid */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <SourceCardSkeleton />
          <SourceCardSkeleton />
          <SourceCardSkeleton />
        </div>
      ) : sources.length === 0 ? (
        <EmptyState
          icon="📚"
          title="No sources yet"
          description="Add a documentation source to start building your knowledge graph"
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {sources.map((source) => (
            <SourceCard
              key={source.id}
              source={source}
              onCrawl={handleCrawl}
              onDelete={handleDelete}
              isCrawling={crawlSource.isPending}
            />
          ))}
        </div>
      )}

      {/* Status indicators */}
      {(createSource.isPending || deleteSource.isPending || crawlSource.isPending) && (
        <div className="fixed bottom-4 right-4 bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-lg px-4 py-2 text-sm text-sc-fg-muted shadow-lg">
          {createSource.isPending && 'Adding source...'}
          {deleteSource.isPending && 'Deleting source...'}
          {crawlSource.isPending && 'Starting crawl...'}
        </div>
      )}
    </div>
  );
}
