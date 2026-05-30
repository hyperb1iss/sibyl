'use client';

import { useState } from 'react';
import { Button } from '@/components/ui';
import { Command, EditPencil, Send, User } from '@/components/ui/icons';
import { formatDistanceToNow } from '@/lib/constants';
import { useAddTaskNote, useTaskNotes } from '@/lib/hooks';

interface TaskNotesSectionProps {
  taskId: string;
}

/**
 * Notes section for task detail page.
 * Shows all notes and provides an input to add new notes.
 */
export function TaskNotesSection({ taskId }: TaskNotesSectionProps) {
  const [content, setContent] = useState('');
  const { data, isLoading } = useTaskNotes(taskId);
  const addNote = useAddTaskNote();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!content.trim()) return;

    await addNote.mutateAsync({
      taskId,
      data: {
        content: content.trim(),
        author_type: 'user',
        author_name: '',
      },
    });
    setContent('');
  };

  const notes = data?.notes ?? [];

  return (
    <div className="bg-sc-bg-elevated border border-sc-fg-subtle/20 rounded-xl p-6">
      <h2 className="text-sm font-semibold text-sc-fg-secondary uppercase tracking-wide mb-4 flex items-center gap-2">
        <EditPencil width={16} height={16} />
        Notes
        {notes.length > 0 && (
          <span className="text-xs font-normal text-sc-fg-muted bg-sc-bg-highlight px-2 py-0.5 rounded-full">
            {notes.length}
          </span>
        )}
      </h2>

      {/* Add note form */}
      <form onSubmit={handleSubmit} className="mb-4">
        <div className="flex gap-2">
          <input
            type="text"
            value={content}
            onChange={e => setContent(e.target.value)}
            placeholder="Add a note..."
            aria-label="Add a note"
            title="Add a note"
            className="flex-1 px-3 py-2 text-sm bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg
                       text-sc-fg-primary placeholder:text-sc-fg-subtle
                       transition-colors duration-200
                       focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            disabled={addNote.isPending}
          />
          <Button
            type="submit"
            size="md"
            disabled={!content.trim() || addNote.isPending}
            aria-label="Send note"
            title="Send note"
            icon={<Send width={14} height={14} />}
          >
            Send
          </Button>
        </div>
      </form>

      {/* Notes list */}
      {isLoading ? (
        <div className="text-sm text-sc-fg-muted animate-pulse">Loading notes...</div>
      ) : notes.length === 0 ? (
        <p className="text-sm text-sc-fg-muted italic">No notes yet. Add one above!</p>
      ) : (
        <div className="space-y-3 max-h-80 overflow-y-auto">
          {notes.map(note => (
            <div
              key={note.id}
              className="p-3 bg-sc-bg-highlight rounded-lg border border-sc-fg-subtle/10"
            >
              <div className="flex items-start gap-2">
                {/* Author icon */}
                <div
                  className={`p-1.5 rounded-full ${
                    note.author_type === 'agent'
                      ? 'bg-sc-cyan/10 text-sc-cyan'
                      : 'bg-sc-purple/10 text-sc-purple'
                  }`}
                >
                  {note.author_type === 'agent' ? (
                    <Command width={14} height={14} />
                  ) : (
                    <User width={14} height={14} />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  {/* Header */}
                  <div className="flex items-center gap-2 mb-1">
                    {note.author_name && (
                      <span
                        className={`text-xs font-medium ${
                          note.author_type === 'agent' ? 'text-sc-cyan' : 'text-sc-purple'
                        }`}
                      >
                        {note.author_name}
                      </span>
                    )}
                    <span className="text-xs text-sc-fg-muted">
                      {formatDistanceToNow(note.created_at)}
                    </span>
                  </div>
                  {/* Content */}
                  <p className="text-sm text-sc-fg-primary whitespace-pre-wrap">{note.content}</p>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
