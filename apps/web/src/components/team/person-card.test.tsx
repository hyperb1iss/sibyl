import { describe, expect, it, vi } from 'vitest';
import type { TeamActivityPerson } from '@/lib/api/activity';
import { render, screen } from '@/test/utils';
import { PersonCard } from './person-card';

const PERSON: TeamActivityPerson = {
  user_id: 'u-grace',
  name: 'Grace Hopper',
  email: null,
  avatar_url: null,
  role: 'member',
  kind: 'user',
  sponsor_user_id: null,
  counts: {
    captures: 1,
    tasks_created: 0,
    tasks_completed: 0,
    decisions: 0,
    notes: 0,
    procedures: 0,
    other: 0,
  },
  last_active_at: '2026-11-05T10:00:00Z',
};

describe('PersonCard', () => {
  it("links the member's inbox to Net, above the card-wide filter button", async () => {
    const onToggle = vi.fn();
    const { user } = render(
      <PersonCard
        person={PERSON}
        maxTotal={1}
        windowPhrase="the last 7 days"
        selected={false}
        isYou={false}
        onToggle={onToggle}
        inbox="@grace"
      />
    );

    const link = screen.getByRole('link', { name: '@grace' });
    expect(link).toHaveAttribute('href', '/net?channel=%40grace');
    expect(link.className).toContain('z-10');
    await user.click(screen.getByRole('button', { name: 'Grace Hopper' }));
    expect(onToggle).toHaveBeenCalledWith('u-grace');
  });

  it('shows no handle until the directory has one', () => {
    render(
      <PersonCard
        person={PERSON}
        maxTotal={1}
        windowPhrase="the last 7 days"
        selected={false}
        isYou={false}
        onToggle={vi.fn()}
      />
    );
    expect(screen.queryByRole('link')).toBeNull();
  });
});
