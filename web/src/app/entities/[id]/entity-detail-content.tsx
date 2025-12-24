'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { EntityBreadcrumb } from '@/components/layout/breadcrumb';
import { EntityBadge } from '@/components/ui/badge';
import { Button, ColorButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input, Textarea } from '@/components/ui/input';
import type { Entity } from '@/lib/api';
import { ENTITY_COLORS, type EntityType, formatDateTime } from '@/lib/constants';
import { useDeleteEntity, useEntity, useUpdateEntity } from '@/lib/hooks';

interface EntityDetailContentProps {
  initialEntity: Entity;
}

export function EntityDetailContent({ initialEntity }: EntityDetailContentProps) {
  const router = useRouter();
  const entityId = initialEntity.id;

  // Hydrate from server data, then use client cache
  const { data: entity } = useEntity(entityId, initialEntity);
  const updateEntity = useUpdateEntity();
  const deleteEntity = useDeleteEntity();

  const [isEditing, setIsEditing] = useState(false);
  const [editedName, setEditedName] = useState('');
  const [editedDescription, setEditedDescription] = useState('');
  const [editedContent, setEditedContent] = useState('');

  // Use entity from query (may be more up-to-date) or fall back to initial
  const currentEntity = entity ?? initialEntity;
  const color = ENTITY_COLORS[currentEntity.entity_type as EntityType] ?? '#8b85a0';

  const handleStartEdit = () => {
    setEditedName(currentEntity.name);
    setEditedDescription(currentEntity.description || '');
    setEditedContent(currentEntity.content || '');
    setIsEditing(true);
  };

  const handleSave = async () => {
    await updateEntity.mutateAsync({
      id: entityId,
      updates: {
        name: editedName,
        description: editedDescription,
        content: editedContent,
      },
    });
    setIsEditing(false);
  };

  const handleCancel = () => {
    setIsEditing(false);
  };

  const handleDelete = async () => {
    if (confirm('Are you sure you want to delete this entity? This action cannot be undone.')) {
      await deleteEntity.mutateAsync(entityId);
      router.push('/entities');
    }
  };

  return (
    <div className="space-y-4 animate-fade-in">
      <EntityBreadcrumb entityType="entity" entityName={currentEntity.name} />

      {/* Hero Header */}
      <div
        className="relative overflow-hidden rounded-xl border"
        style={{
          background: `linear-gradient(135deg, ${color}18 0%, var(--sc-bg-base) 50%, var(--sc-bg-base) 100%)`,
          borderColor: `${color}40`,
        }}
      >
        {/* Accent bar */}
        <div className="absolute left-0 top-0 bottom-0 w-1.5" style={{ backgroundColor: color }} />

        <div className="pl-5 pr-4 py-5">
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1 min-w-0">
              {/* Type Badge */}
              <div className="flex items-center gap-3 mb-3">
                <EntityBadge type={currentEntity.entity_type} size="md" showIcon />
              </div>

              {/* Title */}
              {isEditing ? (
                <Input
                  type="text"
                  value={editedName}
                  onChange={e => setEditedName(e.target.value)}
                  className="text-2xl font-bold"
                />
              ) : (
                <h1 className="text-2xl font-bold text-sc-fg-primary">{currentEntity.name}</h1>
              )}
            </div>

            {/* Actions */}
            <div className="flex items-center gap-2 shrink-0">
              {isEditing ? (
                <>
                  <Button variant="secondary" onClick={handleCancel}>
                    Cancel
                  </Button>
                  <ColorButton color="green" onClick={handleSave} disabled={updateEntity.isPending}>
                    {updateEntity.isPending ? 'Saving...' : 'Save'}
                  </ColorButton>
                </>
              ) : (
                <>
                  <Link href={`/graph?selected=${entityId}`}>
                    <ColorButton color="purple" icon="⬡">
                      View in Graph
                    </ColorButton>
                  </Link>
                  <ColorButton color="cyan" onClick={handleStartEdit}>
                    Edit
                  </ColorButton>
                  <ColorButton color="red" onClick={handleDelete} disabled={deleteEntity.isPending}>
                    {deleteEntity.isPending ? 'Deleting...' : 'Delete'}
                  </ColorButton>
                </>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Content Section */}
        <div className="lg:col-span-2 space-y-6">
          {/* Description */}
          <Card>
            <h2 className="text-lg font-semibold text-sc-fg-primary mb-4">Description</h2>
            {isEditing ? (
              <Textarea
                value={editedDescription}
                onChange={e => setEditedDescription(e.target.value)}
                rows={3}
                placeholder="Enter description..."
              />
            ) : (
              <p className="text-sc-fg-muted whitespace-pre-wrap">
                {currentEntity.description || 'No description available'}
              </p>
            )}
          </Card>

          {/* Content */}
          <Card>
            <h2 className="text-lg font-semibold text-sc-fg-primary mb-4">Content</h2>
            {isEditing ? (
              <Textarea
                value={editedContent}
                onChange={e => setEditedContent(e.target.value)}
                rows={12}
                monospace
                placeholder="Enter content..."
              />
            ) : (
              <pre className="font-mono text-sm text-sc-fg-muted whitespace-pre-wrap overflow-x-auto">
                {currentEntity.content || 'No content available'}
              </pre>
            )}
          </Card>
        </div>

        {/* Metadata Sidebar */}
        <div className="space-y-6">
          {/* Details */}
          <Card>
            <h2 className="text-lg font-semibold text-sc-fg-primary mb-4">Details</h2>
            <dl className="space-y-3">
              <div>
                <dt className="text-xs text-sc-fg-subtle uppercase tracking-wide">ID</dt>
                <dd className="text-sc-fg-muted font-mono text-sm mt-1 break-all">
                  {currentEntity.id}
                </dd>
              </div>
              {currentEntity.source_file && (
                <div>
                  <dt className="text-xs text-sc-fg-subtle uppercase tracking-wide">Source File</dt>
                  <dd className="text-sc-fg-muted font-mono text-sm mt-1 break-all">
                    {currentEntity.source_file}
                  </dd>
                </div>
              )}
              {currentEntity.created_at && (
                <div>
                  <dt className="text-xs text-sc-fg-subtle uppercase tracking-wide">Created</dt>
                  <dd className="text-sc-fg-muted text-sm mt-1">
                    {formatDateTime(currentEntity.created_at)}
                  </dd>
                </div>
              )}
              {currentEntity.updated_at && (
                <div>
                  <dt className="text-xs text-sc-fg-subtle uppercase tracking-wide">Updated</dt>
                  <dd className="text-sc-fg-muted text-sm mt-1">
                    {formatDateTime(currentEntity.updated_at)}
                  </dd>
                </div>
              )}
            </dl>
          </Card>

          {/* Metadata */}
          {currentEntity.metadata && Object.keys(currentEntity.metadata).length > 0 && (
            <Card>
              <h2 className="text-lg font-semibold text-sc-fg-primary mb-4">Metadata</h2>
              <pre className="font-mono text-xs text-sc-fg-muted whitespace-pre-wrap overflow-x-auto">
                {JSON.stringify(currentEntity.metadata, null, 2)}
              </pre>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
