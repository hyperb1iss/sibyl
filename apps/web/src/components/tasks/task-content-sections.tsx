'use client';

import Link from 'next/link';
import { useState } from 'react';
import { EditableTags, EditableText } from '@/components/editable';
import { EntityBadge } from '@/components/ui/badge';
import { CheckCircle2, ChevronRight, Hash, Pencil } from '@/components/ui/icons';
import { Markdown } from '@/components/ui/markdown';
import type { Entity } from '@/lib/api';
import type { TaskStatusType } from '@/lib/constants';
import type { RelatedKnowledgeItem } from './task-detail-types';

interface TaskContentSectionsProps {
  task: Entity;
  status: TaskStatusType;
  technologies: string[];
  tags: string[];
  learnings: string | undefined;
  relatedKnowledge: RelatedKnowledgeItem[];
  onUpdateField: (field: string, value: unknown, metadataField?: boolean) => Promise<void>;
}

/**
 * Main content area: Details, Technologies, Tags, Learnings, Related Knowledge.
 */
export function TaskContentSections({
  task,
  status,
  technologies,
  tags,
  learnings,
  relatedKnowledge,
  onUpdateField,
}: TaskContentSectionsProps) {
  const [editingContent, setEditingContent] = useState(false);
  const [editingLearnings, setEditingLearnings] = useState(false);

  const validRelatedKnowledge = relatedKnowledge.filter(item => item.id?.length > 0);

  return (
    <div className="lg:col-span-2 space-y-6">
      {/* Details */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide">
            Details
          </h2>
          <button
            type="button"
            onClick={() => setEditingContent(!editingContent)}
            className={`p-1.5 rounded-lg transition-all ${
              editingContent
                ? 'bg-sc-purple/20 text-sc-purple'
                : 'text-sc-fg-subtle hover:text-sc-fg-muted hover:bg-sc-bg-highlight/50'
            }`}
            title={editingContent ? 'View markdown' : 'Edit'}
          >
            <Pencil width={14} height={14} />
          </button>
        </div>
        {editingContent ? (
          <EditableText
            value={task.content || ''}
            onSave={async v => {
              await onUpdateField('content', v || undefined, false);
              setEditingContent(false);
            }}
            placeholder="Add detailed content, requirements, notes... (Markdown supported)"
            multiline
            rows={10}
          />
        ) : task.content ? (
          <Markdown content={task.content} />
        ) : (
          <button
            type="button"
            onClick={() => setEditingContent(true)}
            className="text-sc-fg-subtle italic hover:text-sc-fg-muted transition-colors"
          >
            Add detailed content, requirements, notes...
          </button>
        )}
      </div>

      {/* Technologies */}
      <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
        <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
          Technologies
        </h2>
        <EditableTags
          values={technologies}
          onSave={v => onUpdateField('technologies', v.length > 0 ? v : undefined)}
          tagClassName="bg-sc-cyan/10 text-sc-cyan border-sc-cyan/20"
          placeholder="Add technology"
          suggestions={['React', 'TypeScript', 'Python', 'Next.js', 'GraphQL', 'Tailwind']}
        />
      </div>

      {/* Tags */}
      <div className="bg-gradient-to-br from-sc-bg-base to-sc-purple/5 border border-sc-purple/20 rounded-2xl p-6">
        <h2 className="text-sm font-semibold text-sc-purple uppercase tracking-wide mb-4 flex items-center gap-2">
          <Hash width={16} height={16} />
          Tags
        </h2>
        <EditableTags
          values={tags}
          onSave={v => onUpdateField('tags', v.length > 0 ? v : undefined)}
          tagClassName="bg-sc-purple/10 text-sc-purple border-sc-purple/20"
          placeholder="Add tag"
          addPlaceholder="Type tag and press Enter"
          suggestions={[
            'frontend',
            'backend',
            'database',
            'devops',
            'testing',
            'docs',
            'security',
            'performance',
            'feature',
            'bug',
            'refactor',
            'chore',
            'research',
          ]}
        />
        {tags.length === 0 && (
          <p className="text-xs text-sc-fg-subtle mt-3 italic">
            Tags are auto-generated when creating tasks. Add more to help organize and filter.
          </p>
        )}
      </div>

      {/* Learnings - show when done or has content */}
      {(status === 'done' || learnings) && (
        <div className="bg-gradient-to-br from-sc-green/10 to-sc-cyan/5 border border-sc-green/20 rounded-2xl p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-sc-green uppercase tracking-wide flex items-center gap-2">
              <CheckCircle2 width={16} height={16} />
              Learnings
            </h2>
            <button
              type="button"
              onClick={() => setEditingLearnings(!editingLearnings)}
              className={`p-1.5 rounded-lg transition-all ${
                editingLearnings
                  ? 'bg-sc-green/20 text-sc-green'
                  : 'text-sc-fg-subtle hover:text-sc-fg-muted hover:bg-sc-bg-highlight/50'
              }`}
              title={editingLearnings ? 'View markdown' : 'Edit'}
            >
              <Pencil width={14} height={14} />
            </button>
          </div>
          {editingLearnings ? (
            <EditableText
              value={learnings || ''}
              onSave={async v => {
                await onUpdateField('learnings', v || undefined);
                setEditingLearnings(false);
              }}
              placeholder="What did you learn? Capture insights... (Markdown supported)"
              multiline
              rows={6}
            />
          ) : learnings ? (
            <Markdown content={learnings} />
          ) : (
            <button
              type="button"
              onClick={() => setEditingLearnings(true)}
              className="text-sc-fg-subtle italic hover:text-sc-fg-muted transition-colors"
            >
              What did you learn? Capture insights...
            </button>
          )}
        </div>
      )}

      {/* Related Knowledge */}
      {validRelatedKnowledge.length > 0 && (
        <div className="bg-sc-bg-base border border-sc-fg-subtle/20 rounded-2xl p-6">
          <h2 className="text-sm font-semibold text-sc-fg-subtle uppercase tracking-wide mb-4">
            Linked Knowledge
          </h2>
          <div className="space-y-2">
            {validRelatedKnowledge.map(item => (
              <Link
                key={item.id}
                href={`/entities/${item.id}`}
                className="flex items-center gap-3 p-3 bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/10 hover:border-sc-purple/30 hover:bg-sc-bg-highlight transition-all group"
              >
                <EntityBadge type={item.type} size="sm" />
                <div className="flex-1 min-w-0">
                  <span className="text-sm text-sc-fg-primary truncate block group-hover:text-sc-purple transition-colors">
                    {item.name}
                  </span>
                  <span className="text-xs text-sc-fg-subtle">{item.relationship}</span>
                </div>
                <ChevronRight
                  width={16}
                  height={16}
                  className="text-sc-fg-subtle group-hover:text-sc-purple transition-colors"
                />
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
