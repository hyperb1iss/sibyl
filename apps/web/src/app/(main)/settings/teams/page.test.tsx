import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useCreateOrgInvitation: vi.fn(),
  useDeleteOrgInvitation: vi.fn(),
  useMe: vi.fn(),
  useOrgInvitations: vi.fn(),
  useOrgMembers: vi.fn(),
  useOrgs: vi.fn(),
  useRemoveOrgMember: vi.fn(),
  useSwitchOrg: vi.fn(),
  useUpdateOrgMemberRole: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);

import TeamsPage from './page';

describe('TeamsPage', () => {
  const createInvitation = { isPending: false, mutateAsync: vi.fn() };

  beforeEach(() => {
    createInvitation.mutateAsync.mockReset();
    createInvitation.mutateAsync.mockResolvedValue({
      invitation: { accept_url: 'https://sibyl.test/invitations/invite-1/accept' },
    });
    hooks.useCreateOrgInvitation.mockReturnValue(createInvitation);
    hooks.useDeleteOrgInvitation.mockReturnValue({ isPending: false, mutateAsync: vi.fn() });
    hooks.useMe.mockReturnValue({
      data: {
        user: { id: 'user-1', email: 'stef@hyperbliss.tech', name: 'Stefanie Jane' },
        organization: { id: 'org-1' },
      },
    });
    hooks.useOrgInvitations.mockReturnValue({
      data: { invitations: [] },
      isLoading: false,
    });
    hooks.useOrgMembers.mockReturnValue({
      data: {
        members: [
          {
            user: {
              id: 'user-1',
              github_id: null,
              email: 'stef@hyperbliss.tech',
              name: 'Stefanie Jane',
              avatar_url: null,
            },
            role: 'owner',
            created_at: '2026-06-01T00:00:00Z',
          },
        ],
      },
      isLoading: false,
    });
    hooks.useOrgs.mockReturnValue({
      data: {
        orgs: [
          {
            id: 'org-1',
            slug: 'u-stefanie',
            name: 'Stefanie Jane',
            is_personal: true,
            role: 'owner',
          },
        ],
      },
      isLoading: false,
      error: null,
    });
    hooks.useRemoveOrgMember.mockReturnValue({ isPending: false, mutateAsync: vi.fn() });
    hooks.useSwitchOrg.mockReturnValue({ isPending: false, mutateAsync: vi.fn() });
    hooks.useUpdateOrgMemberRole.mockReturnValue({ mutateAsync: vi.fn() });
  });

  it('keeps the invite email field usable and enables invite after typing', async () => {
    const { user } = render(<TeamsPage />);

    const emailInput = screen.getByRole('textbox', { name: 'Invite email for Stefanie Jane' });
    const roleSelect = screen.getByRole('combobox', { name: 'Invite role' });
    const inviteButton = screen.getByRole('button', { name: 'Invite' });
    const inviteForm = emailInput.closest('form');

    expect(inviteForm).toHaveClass('grid');
    expect(inviteForm).toHaveClass('sm:grid-cols-[minmax(12rem,1fr)_180px_auto]');
    expect(emailInput).toHaveClass('w-full');
    expect(roleSelect).toHaveTextContent('member');
    expect(inviteButton).toBeDisabled();

    await user.click(roleSelect);
    await user.click(
      within(await screen.findByRole('listbox')).getByRole('option', { name: 'admin' })
    );
    await user.type(emailInput, 'teammate@example.com');

    expect(roleSelect).toHaveTextContent('admin');
    expect(inviteButton).toBeEnabled();

    await user.click(inviteButton);

    expect(createInvitation.mutateAsync).toHaveBeenCalledWith({
      slug: 'u-stefanie',
      email: 'teammate@example.com',
      role: 'admin',
    });
  });
});
