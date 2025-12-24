import { describe, expect, it } from 'vitest';
import { render, screen } from '@/test/utils';
import { EntityBadge, StatusBadge } from './badge';

describe('EntityBadge', () => {
  it('renders entity type as text', () => {
    render(<EntityBadge type="pattern" />);
    expect(screen.getByText('pattern')).toBeInTheDocument();
  });

  it('converts underscores to spaces in type name', () => {
    render(<EntityBadge type="error_pattern" />);
    expect(screen.getByText('error pattern')).toBeInTheDocument();
  });

  it('applies size classes correctly', () => {
    const { container } = render(<EntityBadge type="task" size="lg" />);
    const badge = container.querySelector('span');
    expect(badge).toHaveClass('px-3', 'py-1.5');
  });

  it('accepts custom className', () => {
    const { container } = render(<EntityBadge type="pattern" className="custom-class" />);
    const badge = container.querySelector('span');
    expect(badge).toHaveClass('custom-class');
  });

  it('renders icon when showIcon is true', () => {
    const { container } = render(<EntityBadge type="pattern" showIcon />);
    // EntityIcon renders an SVG
    const svg = container.querySelector('svg');
    expect(svg).toBeInTheDocument();
  });

  it('does not render icon by default', () => {
    const { container } = render(<EntityBadge type="pattern" />);
    const svg = container.querySelector('svg');
    expect(svg).not.toBeInTheDocument();
  });
});

describe('StatusBadge', () => {
  it('renders status label', () => {
    render(<StatusBadge status="healthy" />);
    expect(screen.getByText('Healthy')).toBeInTheDocument();
  });

  it('uses custom label when provided', () => {
    render(<StatusBadge status="healthy" label="All Good" />);
    expect(screen.getByText('All Good')).toBeInTheDocument();
  });

  it('applies pulse animation when pulse is true', () => {
    const { container } = render(<StatusBadge status="running" pulse />);
    const dot = container.querySelector('.animate-pulse');
    expect(dot).toBeInTheDocument();
  });

  it('does not pulse by default', () => {
    const { container } = render(<StatusBadge status="running" />);
    const dot = container.querySelector('.animate-pulse');
    expect(dot).not.toBeInTheDocument();
  });

  it.each([
    'healthy',
    'unhealthy',
    'warning',
    'idle',
    'running',
  ] as const)('renders %s status correctly', status => {
    render(<StatusBadge status={status} />);
    const capitalizedStatus = status.charAt(0).toUpperCase() + status.slice(1);
    expect(screen.getByText(capitalizedStatus)).toBeInTheDocument();
  });
});
