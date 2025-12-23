'use client';

import Link from 'next/link';
import { use, useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Breadcrumb, ROUTE_CONFIG } from '@/components/layout/breadcrumb';
import {
  ArrowLeft,
  Calendar,
  Check,
  ChevronRight,
  ExternalLink,
  FileText,
  Hash,
  Loader2,
  Network,
  Pencil,
  X,
} from '@/components/ui/icons';
import { Markdown } from '@/components/ui/markdown';
import { ENTITY_STYLES, formatDateTime } from '@/lib/constants';
import { useDocumentEntities, useFullPage, useUpdateDocument } from '@/lib/hooks';

interface DocumentDetailPageProps {
  params: Promise<{ id: string; docId: string }>;
}

export default function DocumentDetailPage({ params }: DocumentDetailPageProps) {
  const { id: sourceId, docId } = use(params);
  const { data: document, isLoading, error } = useFullPage(docId);
  const { data: entitiesData } = useDocumentEntities(docId);
  const updateDocument = useUpdateDocument();

  const [isEditingTitle, setIsEditingTitle] = useState(false);
  const [isEditingContent, setIsEditingContent] = useState(false);
  const [editedTitle, setEditedTitle] = useState('');
  const [editedContent, setEditedContent] = useState('');
  const titleInputRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLTextAreaElement>(null);

  // Sync state when document loads
  useEffect(() => {
    if (document) {
      setEditedTitle(document.title);
      setEditedContent(document.content);
    }
  }, [document]);

  // Focus inputs when edit mode starts
  useEffect(() => {
    if (isEditingTitle && titleInputRef.current) {
      titleInputRef.current.focus();
      titleInputRef.current.select();
    }
  }, [isEditingTitle]);

  useEffect(() => {
    if (isEditingContent && contentRef.current) {
      contentRef.current.focus();
    }
  }, [isEditingContent]);

  const handleSaveTitle = useCallback(async () => {
    if (!document || editedTitle === document.title) {
      setIsEditingTitle(false);
      return;
    }
    try {
      await updateDocument.mutateAsync({
        documentId: docId,
        updates: { title: editedTitle },
      });
      toast.success('Title updated');
      setIsEditingTitle(false);
    } catch {
      toast.error('Failed to update title');
      setEditedTitle(document.title);
    }
  }, [docId, editedTitle, document, updateDocument]);

  const handleSaveContent = useCallback(async () => {
    if (!document || editedContent === document.content) {
      setIsEditingContent(false);
      return;
    }
    try {
      await updateDocument.mutateAsync({
        documentId: docId,
        updates: { content: editedContent },
      });
      toast.success('Content updated');
      setIsEditingContent(false);
    } catch {
      toast.error('Failed to update content');
      setEditedContent(document.content);
    }
  }, [docId, editedContent, document, updateDocument]);

  const handleCancelTitle = useCallback(() => {
    setEditedTitle(document?.title || '');
    setIsEditingTitle(false);
  }, [document]);

  const handleCancelContent = useCallback(() => {
    setEditedContent(document?.content || '');
    setIsEditingContent(false);
  }, [document]);

  const breadcrumbItems = [
    { label: ROUTE_CONFIG[''].label, href: '/', icon: ROUTE_CONFIG[''].icon },
    { label: 'Sources', href: '/sources' },
    { label: document?.source_name || 'Source', href: `/sources/${sourceId}` },
    { label: document?.title || 'Document', href: `/sources/${sourceId}/documents/${docId}` },
  ];

  if (error) {
    return (
      <div className="space-y-4 animate-fade-in">
        <Breadcrumb items={breadcrumbItems} />
        <div className="bg-sc-bg-base border border-sc-red/30 rounded-2xl p-8 text-center">
          <p className="text-sc-red font-medium">Failed to load document</p>
          <p className="text-sc-fg-subtle text-sm mt-2">
            {error instanceof Error ? error.message : 'Unknown error'}
          </p>
          <Link
            href={`/sources/${sourceId}`}
            className="inline-flex items-center gap-2 mt-4 text-sc-cyan hover:text-sc-purple transition-colors"
          >
            <ArrowLeft width={16} height={16} />
            Back to Source
          </Link>
        </div>
      </div>
    );
  }

  if (isLoading || !document) {
    return (
      <div className="space-y-6 animate-fade-in">
        <Breadcrumb items={breadcrumbItems} />
        <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-8">
          <div className="animate-pulse space-y-4">
            <div className="h-8 bg-sc-bg-highlight rounded w-2/3" />
            <div className="h-4 bg-sc-bg-highlight rounded w-1/3" />
            <div className="h-64 bg-sc-bg-highlight rounded-xl mt-6" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <Breadcrumb items={breadcrumbItems} />

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Main Content */}
        <div className="lg:col-span-3 space-y-6">
          {/* Header Card */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-6">
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1 min-w-0">
                {/* Title */}
                <div className="flex items-center gap-3 mb-2">
                  <FileText width={24} height={24} className="text-sc-cyan shrink-0" />
                  {isEditingTitle ? (
                    <div className="flex items-center gap-2 flex-1">
                      <input
                        ref={titleInputRef}
                        type="text"
                        value={editedTitle}
                        onChange={e => setEditedTitle(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Enter') void handleSaveTitle();
                          if (e.key === 'Escape') handleCancelTitle();
                        }}
                        className="flex-1 bg-sc-bg-dark border border-sc-purple/50 rounded-lg px-3 py-1.5 text-xl font-bold text-sc-fg-primary focus:outline-none focus:border-sc-purple"
                      />
                      <button
                        type="button"
                        onClick={handleSaveTitle}
                        disabled={updateDocument.isPending}
                        className="p-1.5 text-sc-green hover:bg-sc-green/10 rounded-lg transition-colors"
                      >
                        <Check width={18} height={18} />
                      </button>
                      <button
                        type="button"
                        onClick={handleCancelTitle}
                        className="p-1.5 text-sc-fg-subtle hover:text-sc-red hover:bg-sc-red/10 rounded-lg transition-colors"
                      >
                        <X width={18} height={18} />
                      </button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 flex-1 group">
                      <h1 className="text-2xl font-bold text-sc-fg-primary truncate">
                        {document.title}
                      </h1>
                      <button
                        type="button"
                        onClick={() => setIsEditingTitle(true)}
                        className="p-1.5 text-sc-fg-subtle opacity-0 group-hover:opacity-100 hover:text-sc-purple hover:bg-sc-purple/10 rounded-lg transition-all"
                      >
                        <Pencil width={14} height={14} />
                      </button>
                    </div>
                  )}
                </div>

                {/* URL */}
                <a
                  href={document.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-sc-cyan hover:text-sc-purple transition-colors text-sm"
                >
                  <ExternalLink width={14} height={14} />
                  {document.url}
                </a>

                {/* Badges */}
                <div className="flex flex-wrap items-center gap-2 mt-3">
                  {document.has_code && (
                    <span className="px-2 py-0.5 text-xs bg-sc-cyan/20 text-sc-cyan rounded">
                      Code
                    </span>
                  )}
                  {document.code_languages.map(lang => (
                    <span
                      key={lang}
                      className="px-2 py-0.5 text-xs bg-sc-purple/20 text-sc-purple rounded"
                    >
                      {lang}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </div>

          {/* Content Card */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl overflow-hidden">
            {/* Content Header */}
            <div className="flex items-center justify-between px-6 py-3 border-b border-sc-fg-subtle/10">
              <h2 className="text-sm font-semibold text-sc-fg-muted uppercase tracking-wide">
                Content
              </h2>
              <button
                type="button"
                onClick={() => {
                  if (isEditingContent) {
                    handleCancelContent();
                  } else {
                    setIsEditingContent(true);
                  }
                }}
                className={`flex items-center gap-2 px-3 py-1.5 text-sm rounded-lg transition-colors ${
                  isEditingContent
                    ? 'bg-sc-bg-highlight text-sc-fg-muted'
                    : 'text-sc-fg-subtle hover:text-sc-purple hover:bg-sc-purple/10'
                }`}
              >
                {isEditingContent ? (
                  <>
                    <X width={14} height={14} />
                    Cancel
                  </>
                ) : (
                  <>
                    <Pencil width={14} height={14} />
                    Edit
                  </>
                )}
              </button>
            </div>

            {/* Content Body */}
            <div className="p-6">
              {isEditingContent ? (
                <div className="space-y-4">
                  <textarea
                    ref={contentRef}
                    value={editedContent}
                    onChange={e => setEditedContent(e.target.value)}
                    className="w-full min-h-[400px] bg-sc-bg-dark border border-sc-fg-subtle/20 rounded-xl p-4 text-sc-fg-primary font-mono text-sm leading-relaxed focus:outline-none focus:border-sc-purple/50 resize-y"
                    placeholder="Document content (Markdown supported)..."
                  />
                  <div className="flex items-center justify-end gap-2">
                    <button
                      type="button"
                      onClick={handleCancelContent}
                      className="px-4 py-2 text-sm text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={handleSaveContent}
                      disabled={updateDocument.isPending}
                      className="flex items-center gap-2 px-4 py-2 text-sm font-medium bg-sc-purple hover:bg-sc-purple/80 text-white rounded-lg transition-colors disabled:opacity-50"
                    >
                      {updateDocument.isPending ? (
                        <Loader2 width={14} height={14} className="animate-spin" />
                      ) : (
                        <Check width={14} height={14} />
                      )}
                      Save Changes
                    </button>
                  </div>
                </div>
              ) : (
                <Markdown content={document.content} className="max-w-none" />
              )}
            </div>
          </div>
        </div>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* Stats Card */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-5">
            <h3 className="text-xs font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
              Document Info
            </h3>
            <div className="space-y-4">
              <StatRow
                icon={<FileText width={14} height={14} className="text-sc-cyan" />}
                label="Words"
                value={document.word_count.toLocaleString()}
              />
              <StatRow
                icon={<Hash width={14} height={14} className="text-sc-coral" />}
                label="Tokens"
                value={document.token_count.toLocaleString()}
              />
              <StatRow
                icon={<Calendar width={14} height={14} className="text-sc-purple" />}
                label="Crawled"
                value={formatDateTime(document.crawled_at)}
              />
            </div>
          </div>

          {/* Outline Card */}
          {document.headings.length > 0 && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-5">
              <h3 className="text-xs font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
                Outline
              </h3>
              <nav className="space-y-1.5">
                {document.headings.slice(0, 15).map((heading, i) => (
                  <div
                    key={`heading-${i}-${heading.slice(0, 20)}`}
                    className="flex items-center gap-2 text-sm text-sc-fg-muted hover:text-sc-cyan transition-colors cursor-pointer truncate"
                  >
                    <ChevronRight width={12} height={12} className="text-sc-fg-subtle shrink-0" />
                    <span className="truncate">{heading}</span>
                  </div>
                ))}
                {document.headings.length > 15 && (
                  <p className="text-xs text-sc-fg-subtle mt-2">
                    +{document.headings.length - 15} more
                  </p>
                )}
              </nav>
            </div>
          )}

          {/* Links Card */}
          {document.links.length > 0 && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-5">
              <h3 className="text-xs font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
                Links ({document.links.length})
              </h3>
              <div className="space-y-1.5 max-h-48 overflow-y-auto">
                {document.links.slice(0, 10).map(link => (
                  <a
                    key={link}
                    href={link}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-2 text-xs text-sc-fg-muted hover:text-sc-cyan transition-colors truncate"
                  >
                    <ExternalLink width={10} height={10} className="shrink-0" />
                    <span className="truncate">{link}</span>
                  </a>
                ))}
                {document.links.length > 10 && (
                  <p className="text-xs text-sc-fg-subtle mt-2">
                    +{document.links.length - 10} more
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Graph Connections */}
          {entitiesData && entitiesData.entities.length > 0 && (
            <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-5">
              <div className="flex items-center gap-2 mb-4">
                <Network width={14} height={14} className="text-sc-purple" />
                <h3 className="text-xs font-semibold text-sc-fg-subtle uppercase tracking-wide">
                  Graph Connections ({entitiesData.total})
                </h3>
              </div>
              <div className="space-y-2 max-h-64 overflow-y-auto">
                {entitiesData.entities.slice(0, 12).map(entity => {
                  const style = ENTITY_STYLES[entity.entity_type as keyof typeof ENTITY_STYLES];
                  const dotClass = style?.dot ?? 'bg-sc-fg-subtle';
                  return (
                    <Link
                      key={entity.id}
                      href={`/entities/${entity.id}`}
                      className="flex items-center gap-3 p-2 -mx-2 rounded-lg hover:bg-sc-bg-highlight transition-colors group"
                    >
                      <div className={`w-2 h-2 rounded-full shrink-0 ${dotClass}`} />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm text-sc-fg-primary truncate group-hover:text-sc-cyan transition-colors">
                          {entity.name}
                        </p>
                        <p className="text-xs text-sc-fg-subtle truncate">
                          {entity.entity_type}
                          {entity.chunk_count > 0 && (
                            <span className="text-sc-fg-subtle/60">
                              {' '}
                              · {entity.chunk_count}% match
                            </span>
                          )}
                        </p>
                      </div>
                      <ChevronRight
                        width={12}
                        height={12}
                        className="text-sc-fg-subtle/50 group-hover:text-sc-cyan shrink-0 transition-colors"
                      />
                    </Link>
                  );
                })}
                {entitiesData.entities.length > 12 && (
                  <p className="text-xs text-sc-fg-subtle pt-2">
                    +{entitiesData.entities.length - 12} more
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Actions */}
          <div className="bg-sc-bg-base border border-sc-fg-subtle/10 rounded-2xl p-5">
            <Link
              href={`/sources/${sourceId}`}
              className="flex items-center gap-2 w-full px-4 py-2.5 text-sm text-sc-fg-muted hover:text-sc-purple hover:bg-sc-purple/10 rounded-xl transition-colors"
            >
              <ArrowLeft width={16} height={16} />
              Back to Source
            </Link>
          </div>
        </div>
      </div>
    </div>
  );
}

function StatRow({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-2">
        {icon}
        <span className="text-sm text-sc-fg-subtle">{label}</span>
      </div>
      <span className="text-sm font-medium text-sc-fg-primary">{value}</span>
    </div>
  );
}
