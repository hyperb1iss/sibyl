import { describe, expect, it } from 'vitest';
import { render, screen } from '@/test/utils';
import { MetadataFields } from './metadata-fields';

describe('MetadataFields', () => {
  it('renders scalars as labeled fields with humanized dates', () => {
    render(
      <MetadataFields
        metadata={{
          capture_surface: 'cli',
          added_at: '2026-09-03T21:09:26.847321+00:00',
          _direct_insert: true,
        }}
      />
    );

    expect(screen.getByText('capture surface')).toBeInTheDocument();
    expect(screen.getByText('cli')).toBeInTheDocument();
    expect(screen.getByText('added at')).toBeInTheDocument();
    expect(screen.getByText(/Sep 3, 2026/)).toBeInTheDocument();
    expect(screen.getByText('yes')).toBeInTheDocument();
  });

  it('sorts internal underscore keys after user-facing ones', () => {
    render(<MetadataFields metadata={{ _direct_insert: true, basis: 'observed' }} />);

    const terms = screen.getAllByRole('term').map(node => node.textContent);
    expect(terms).toEqual(['basis', 'direct insert']);
  });

  it('keeps nested values compact and the raw JSON one click away', () => {
    const { container } = render(
      <MetadataFields metadata={{ embedding: { model: 'text-embedding-3-small' } }} />
    );

    const compact = container.querySelector('dd code');
    expect(compact?.textContent).toContain('"model": "text-embedding-3-small"');
    expect(screen.getByText('Raw JSON')).toBeInTheDocument();
    expect(container.querySelector('details pre')?.textContent).toContain('"embedding"');
  });

  it('renders nothing for empty metadata', () => {
    const { container } = render(<MetadataFields metadata={{}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
