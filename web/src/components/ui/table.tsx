'use client';

import {
  forwardRef,
  type HTMLAttributes,
  type TdHTMLAttributes,
  type ThHTMLAttributes,
} from 'react';
import { NavArrowDown, NavArrowUp } from '@/components/ui/icons';

// Table container with horizontal scroll
interface TableProps extends HTMLAttributes<HTMLTableElement> {
  striped?: boolean;
  compact?: boolean;
}

const Table = forwardRef<HTMLTableElement, TableProps>(
  ({ className = '', striped = false, compact = false, ...props }, ref) => (
    <div className="relative w-full overflow-auto rounded-lg border border-sc-fg-subtle/20">
      <table
        ref={ref}
        data-striped={striped}
        data-compact={compact}
        className={`w-full caption-bottom text-sm ${className}`}
        {...props}
      />
    </div>
  )
);
Table.displayName = 'Table';

// Table header section
const TableHeader = forwardRef<HTMLTableSectionElement, HTMLAttributes<HTMLTableSectionElement>>(
  ({ className = '', ...props }, ref) => (
    <thead
      ref={ref}
      className={`bg-sc-bg-highlight/50 [&_tr]:border-b [&_tr]:border-sc-fg-subtle/20 ${className}`}
      {...props}
    />
  )
);
TableHeader.displayName = 'TableHeader';

// Table body section
const TableBody = forwardRef<HTMLTableSectionElement, HTMLAttributes<HTMLTableSectionElement>>(
  ({ className = '', ...props }, ref) => (
    <tbody
      ref={ref}
      className={`
        [&_tr:last-child]:border-0
        [&_tr]:border-b [&_tr]:border-sc-fg-subtle/10
        [table[data-striped=true]_&_tr:nth-child(even)]:bg-sc-bg-highlight/30
        ${className}
      `}
      {...props}
    />
  )
);
TableBody.displayName = 'TableBody';

// Table footer section
const TableFooter = forwardRef<HTMLTableSectionElement, HTMLAttributes<HTMLTableSectionElement>>(
  ({ className = '', ...props }, ref) => (
    <tfoot
      ref={ref}
      className={`border-t border-sc-fg-subtle/20 bg-sc-bg-highlight/30 font-medium ${className}`}
      {...props}
    />
  )
);
TableFooter.displayName = 'TableFooter';

// Table row
interface TableRowProps extends HTMLAttributes<HTMLTableRowElement> {
  interactive?: boolean;
  selected?: boolean;
}

const TableRow = forwardRef<HTMLTableRowElement, TableRowProps>(
  ({ className = '', interactive = false, selected = false, ...props }, ref) => (
    <tr
      ref={ref}
      data-selected={selected}
      className={`
        transition-colors duration-150
        ${interactive ? 'cursor-pointer hover:bg-sc-bg-highlight/50' : ''}
        ${selected ? 'bg-sc-purple/10 hover:bg-sc-purple/15' : ''}
        ${className}
      `}
      {...props}
    />
  )
);
TableRow.displayName = 'TableRow';

// Table header cell with sort support
type SortDirection = 'asc' | 'desc' | null;

interface TableHeadProps extends ThHTMLAttributes<HTMLTableCellElement> {
  sortable?: boolean;
  sortDirection?: SortDirection;
  onSort?: () => void;
}

const TableHead = forwardRef<HTMLTableCellElement, TableHeadProps>(
  ({ className = '', sortable = false, sortDirection = null, onSort, children, ...props }, ref) => {
    const content = sortable ? (
      <button
        type="button"
        onClick={onSort}
        className={`
          inline-flex items-center gap-1.5 -mx-2 px-2 py-1 rounded
          transition-colors duration-150
          hover:bg-sc-bg-highlight/50
          focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
          ${sortDirection ? 'text-sc-purple' : ''}
        `}
      >
        {children}
        <span className="text-sc-fg-muted">
          {sortDirection === 'asc' && <NavArrowUp className="h-4 w-4" />}
          {sortDirection === 'desc' && <NavArrowDown className="h-4 w-4" />}
          {!sortDirection && (
            <span className="h-4 w-4 opacity-0 group-hover:opacity-50 transition-opacity">
              <NavArrowUp className="h-4 w-4" />
            </span>
          )}
        </span>
      </button>
    ) : (
      children
    );

    return (
      <th
        ref={ref}
        className={`
          h-11 px-4 text-left align-middle font-medium text-sc-fg-muted
          [table[data-compact=true]_&]:h-9 [table[data-compact=true]_&]:px-3
          [&:has([role=checkbox])]:pr-0
          ${sortable ? 'group' : ''}
          ${className}
        `}
        {...props}
      >
        {content}
      </th>
    );
  }
);
TableHead.displayName = 'TableHead';

// Table data cell
const TableCell = forwardRef<HTMLTableCellElement, TdHTMLAttributes<HTMLTableCellElement>>(
  ({ className = '', ...props }, ref) => (
    <td
      ref={ref}
      className={`
        px-4 py-3 align-middle text-sc-fg-primary
        [table[data-compact=true]_&]:px-3 [table[data-compact=true]_&]:py-2
        [&:has([role=checkbox])]:pr-0
        ${className}
      `}
      {...props}
    />
  )
);
TableCell.displayName = 'TableCell';

// Table caption
const TableCaption = forwardRef<HTMLTableCaptionElement, HTMLAttributes<HTMLTableCaptionElement>>(
  ({ className = '', ...props }, ref) => (
    <caption ref={ref} className={`mt-4 text-sm text-sc-fg-muted ${className}`} {...props} />
  )
);
TableCaption.displayName = 'TableCaption';

// Empty state for tables
interface TableEmptyProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
}

function TableEmpty({ icon, title, description, action }: TableEmptyProps) {
  return (
    <TableRow>
      <TableCell colSpan={100} className="h-48">
        <div className="flex flex-col items-center justify-center text-center">
          {icon && <div className="text-4xl text-sc-fg-subtle mb-3">{icon}</div>}
          <h3 className="text-lg font-medium text-sc-fg-primary mb-1">{title}</h3>
          {description && <p className="text-sm text-sc-fg-muted max-w-sm">{description}</p>}
          {action && <div className="mt-4">{action}</div>}
        </div>
      </TableCell>
    </TableRow>
  );
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
  TableEmpty,
  type SortDirection,
};
