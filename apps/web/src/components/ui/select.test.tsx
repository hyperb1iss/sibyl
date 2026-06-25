import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@/test/utils';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './select';

function RoleSelectHarness() {
  const [role, setRole] = useState('member');

  return (
    <>
      <Select value={role} onValueChange={setRole}>
        <SelectTrigger aria-label="Invite role">
          <SelectValue />
        </SelectTrigger>
        <SelectContent data-testid="select-content">
          <SelectItem value="member">member</SelectItem>
          <SelectItem value="admin">admin</SelectItem>
        </SelectContent>
      </Select>
      <output aria-label="Selected role">{role}</output>
    </>
  );
}

describe('Select', () => {
  it('opens a full option list and allows choosing another role', async () => {
    const { user } = render(<RoleSelectHarness />);

    await user.click(screen.getByRole('combobox', { name: 'Invite role' }));

    const content = await screen.findByTestId('select-content');
    expect(content).toHaveClass('overflow-y-auto');
    expect(content).not.toHaveClass('overflow-hidden');
    expect(content.querySelector('[data-radix-select-viewport]')).not.toHaveClass(
      'h-[var(--radix-select-trigger-height)]'
    );

    await user.click(within(content).getByRole('option', { name: 'admin' }));

    expect(screen.getByRole('status', { name: 'Selected role' })).toHaveTextContent('admin');
  });
});
