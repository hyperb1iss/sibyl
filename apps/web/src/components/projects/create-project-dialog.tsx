'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { FolderKanban } from '@/components/ui/icons';
import { useCreateEntity } from '@/lib/hooks';

interface CreateProjectDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onCreated?: (projectId: string) => void;
}

export function CreateProjectDialog({ isOpen, onClose, onCreated }: CreateProjectDialogProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const createEntity = useCreateEntity();

  // Reset form when opened
  useEffect(() => {
    if (isOpen) {
      setName('');
      setDescription('');
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [isOpen]);

  const handleSubmit = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault();
      if (!name.trim() || createEntity.isPending) return;

      try {
        const project = await createEntity.mutateAsync({
          name: name.trim(),
          description: description.trim() || undefined,
          entity_type: 'project',
        });
        toast.success(`Project "${project.name}" created`);
        onClose();
        onCreated?.(project.id);
      } catch {
        toast.error('Failed to create project');
      }
    },
    [name, description, createEntity, onClose, onCreated]
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit]
  );

  return (
    <Dialog open={isOpen} onOpenChange={open => !open && onClose()}>
      <DialogContent size="md" onKeyDown={handleKeyDown}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FolderKanban width={18} height={18} className="text-sc-cyan" />
            New Project
          </DialogTitle>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div>
            <label htmlFor="create-project-name" className="sr-only">
              Project name
            </label>
            <input
              ref={inputRef}
              id="create-project-name"
              type="text"
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="Project name"
              className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-muted transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base"
            />
          </div>

          {/* Description */}
          <div>
            <label htmlFor="create-project-description" className="sr-only">
              Project description
            </label>
            <textarea
              id="create-project-description"
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Description (optional)"
              rows={3}
              className="w-full px-3 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-muted resize-none transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base"
            />
          </div>

          <DialogFooter className="items-center sm:justify-between">
            <div className="text-xs text-sc-fg-muted">
              <kbd className="bg-sc-bg-highlight px-1.5 py-0.5 rounded">⌘</kbd>
              <span className="mx-1">+</span>
              <kbd className="bg-sc-bg-highlight px-1.5 py-0.5 rounded">↵</kbd>
              <span className="ml-1">to submit</span>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="secondary" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit" loading={createEntity.isPending} disabled={!name.trim()}>
                {createEntity.isPending ? 'Creating...' : 'Create Project'}
              </Button>
            </div>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
