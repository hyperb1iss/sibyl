'use client';

import { ArrowLeft, ArrowRight } from '@/components/ui/icons';

interface PaginationProps {
  currentPage: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  siblingCount?: number;
  showFirstLast?: boolean;
  size?: 'sm' | 'md';
  className?: string;
}

function range(start: number, end: number): number[] {
  const length = end - start + 1;
  return Array.from({ length }, (_, i) => start + i);
}

function generatePagination(
  currentPage: number,
  totalPages: number,
  siblingCount: number
): (number | 'ellipsis')[] {
  const totalPageNumbers = siblingCount * 2 + 5; // siblings + first + last + current + 2 ellipsis

  // If total pages is less than what we'd show, return all pages
  if (totalPages <= totalPageNumbers) {
    return range(1, totalPages);
  }

  const leftSiblingIndex = Math.max(currentPage - siblingCount, 1);
  const rightSiblingIndex = Math.min(currentPage + siblingCount, totalPages);

  const showLeftEllipsis = leftSiblingIndex > 2;
  const showRightEllipsis = rightSiblingIndex < totalPages - 1;

  if (!showLeftEllipsis && showRightEllipsis) {
    const leftItemCount = 3 + 2 * siblingCount;
    const leftRange = range(1, leftItemCount);
    return [...leftRange, 'ellipsis', totalPages];
  }

  if (showLeftEllipsis && !showRightEllipsis) {
    const rightItemCount = 3 + 2 * siblingCount;
    const rightRange = range(totalPages - rightItemCount + 1, totalPages);
    return [1, 'ellipsis', ...rightRange];
  }

  const middleRange = range(leftSiblingIndex, rightSiblingIndex);
  return [1, 'ellipsis', ...middleRange, 'ellipsis', totalPages];
}

const sizes = {
  sm: {
    button: 'h-8 min-w-8 px-2 text-sm',
    icon: 'h-4 w-4',
  },
  md: {
    button: 'h-10 min-w-10 px-3 text-sm',
    icon: 'h-5 w-5',
  },
};

export function Pagination({
  currentPage,
  totalPages,
  onPageChange,
  siblingCount = 1,
  showFirstLast = true,
  size = 'md',
  className = '',
}: PaginationProps) {
  const sizeConfig = sizes[size];
  const pages = generatePagination(currentPage, totalPages, siblingCount);

  const canGoPrev = currentPage > 1;
  const canGoNext = currentPage < totalPages;

  const buttonBase = `
    inline-flex items-center justify-center rounded-lg font-medium
    transition-colors duration-150
    focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
    disabled:opacity-50 disabled:cursor-not-allowed
  `;

  const buttonVariant = (isActive: boolean) =>
    isActive
      ? 'bg-sc-purple text-white'
      : 'bg-sc-bg-highlight text-sc-fg-muted hover:bg-sc-bg-elevated hover:text-sc-fg-primary';

  return (
    <nav aria-label="Pagination" className={`flex items-center gap-1 ${className}`}>
      {/* Previous button */}
      <button
        type="button"
        onClick={() => onPageChange(currentPage - 1)}
        disabled={!canGoPrev}
        className={`${buttonBase} ${sizeConfig.button} ${buttonVariant(false)}`}
        aria-label="Go to previous page"
      >
        <ArrowLeft className={sizeConfig.icon} />
      </button>

      {/* First page */}
      {showFirstLast && pages[0] !== 1 && (
        <>
          <button
            type="button"
            onClick={() => onPageChange(1)}
            className={`${buttonBase} ${sizeConfig.button} ${buttonVariant(currentPage === 1)}`}
            aria-label="Go to page 1"
            aria-current={currentPage === 1 ? 'page' : undefined}
          >
            1
          </button>
          {pages[0] !== 'ellipsis' && pages[0] !== 2 && (
            <span className="px-2 text-sc-fg-subtle">...</span>
          )}
        </>
      )}

      {/* Page numbers */}
      {pages.map((page, index) => {
        if (page === 'ellipsis') {
          return (
            <span key={`ellipsis-${index}`} className="px-2 text-sc-fg-subtle">
              ...
            </span>
          );
        }

        return (
          <button
            key={page}
            type="button"
            onClick={() => onPageChange(page)}
            className={`${buttonBase} ${sizeConfig.button} ${buttonVariant(currentPage === page)}`}
            aria-label={`Go to page ${page}`}
            aria-current={currentPage === page ? 'page' : undefined}
          >
            {page}
          </button>
        );
      })}

      {/* Last page */}
      {showFirstLast && pages[pages.length - 1] !== totalPages && (
        <>
          {pages[pages.length - 1] !== 'ellipsis' && pages[pages.length - 1] !== totalPages - 1 && (
            <span className="px-2 text-sc-fg-subtle">...</span>
          )}
          <button
            type="button"
            onClick={() => onPageChange(totalPages)}
            className={`${buttonBase} ${sizeConfig.button} ${buttonVariant(currentPage === totalPages)}`}
            aria-label={`Go to page ${totalPages}`}
            aria-current={currentPage === totalPages ? 'page' : undefined}
          >
            {totalPages}
          </button>
        </>
      )}

      {/* Next button */}
      <button
        type="button"
        onClick={() => onPageChange(currentPage + 1)}
        disabled={!canGoNext}
        className={`${buttonBase} ${sizeConfig.button} ${buttonVariant(false)}`}
        aria-label="Go to next page"
      >
        <ArrowRight className={sizeConfig.icon} />
      </button>
    </nav>
  );
}

// Simple prev/next pagination for infinite scroll or simple cases
interface SimplePaginationProps {
  hasNext: boolean;
  hasPrev: boolean;
  onNext: () => void;
  onPrev: () => void;
  loading?: boolean;
  size?: 'sm' | 'md';
  className?: string;
}

export function SimplePagination({
  hasNext,
  hasPrev,
  onNext,
  onPrev,
  loading = false,
  size = 'md',
  className = '',
}: SimplePaginationProps) {
  const sizeConfig = sizes[size];

  const buttonBase = `
    inline-flex items-center gap-2 rounded-lg font-medium
    transition-colors duration-150
    bg-sc-bg-highlight text-sc-fg-muted
    hover:bg-sc-bg-elevated hover:text-sc-fg-primary
    focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
    disabled:opacity-50 disabled:cursor-not-allowed
  `;

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      <button
        type="button"
        onClick={onPrev}
        disabled={!hasPrev || loading}
        className={`${buttonBase} ${sizeConfig.button}`}
      >
        <ArrowLeft className={sizeConfig.icon} />
        <span>Previous</span>
      </button>
      <button
        type="button"
        onClick={onNext}
        disabled={!hasNext || loading}
        className={`${buttonBase} ${sizeConfig.button}`}
      >
        <span>Next</span>
        <ArrowRight className={sizeConfig.icon} />
      </button>
    </div>
  );
}

// Page size selector
interface PageSizeSelectorProps {
  value: number;
  onChange: (size: number) => void;
  options?: number[];
  className?: string;
}

export function PageSizeSelector({
  value,
  onChange,
  options = [10, 25, 50, 100],
  className = '',
}: PageSizeSelectorProps) {
  return (
    <div className={`flex items-center gap-2 text-sm ${className}`}>
      <span className="text-sc-fg-muted">Show</span>
      <select
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        className="
          h-9 px-3 rounded-lg
          bg-sc-bg-highlight border border-sc-fg-subtle/20
          text-sc-fg-primary
          transition-colors duration-150
          hover:border-sc-purple/50
          focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
        "
      >
        {options.map(opt => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
      <span className="text-sc-fg-muted">per page</span>
    </div>
  );
}
