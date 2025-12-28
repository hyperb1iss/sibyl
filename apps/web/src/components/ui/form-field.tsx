'use client';

import { type ReactNode, useId } from 'react';

interface FormFieldProps {
  label: string;
  description?: string;
  error?: string;
  required?: boolean;
  children: ReactNode | ((props: { id: string; 'aria-describedby'?: string }) => ReactNode);
  className?: string;
}

export function FormField({
  label,
  description,
  error,
  required,
  children,
  className = '',
}: FormFieldProps) {
  const id = useId();
  const descriptionId = description ? `${id}-description` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const ariaDescribedBy = [descriptionId, errorId].filter(Boolean).join(' ') || undefined;

  return (
    <div className={`space-y-2 ${className}`}>
      <label htmlFor={id} className="block text-sm font-medium text-sc-fg-primary">
        {label}
        {required && <span className="text-sc-red ml-1">*</span>}
      </label>

      {description && (
        <p id={descriptionId} className="text-xs text-sc-fg-muted">
          {description}
        </p>
      )}

      {typeof children === 'function'
        ? children({ id, 'aria-describedby': ariaDescribedBy })
        : children}

      {error && (
        <p id={errorId} className="text-sm text-sc-red flex items-center gap-1.5" role="alert">
          <span className="inline-block w-1 h-1 rounded-full bg-sc-red" />
          {error}
        </p>
      )}
    </div>
  );
}

// Horizontal form field variant
interface FormFieldInlineProps extends Omit<FormFieldProps, 'children'> {
  children: ReactNode;
}

export function FormFieldInline({
  label,
  description,
  error,
  required,
  children,
  className = '',
}: FormFieldInlineProps) {
  const id = useId();
  const errorId = error ? `${id}-error` : undefined;

  return (
    <div className={className}>
      <div className="flex items-center justify-between">
        <div>
          <label htmlFor={id} className="block text-sm font-medium text-sc-fg-primary">
            {label}
            {required && <span className="text-sc-red ml-1">*</span>}
          </label>
          {description && <p className="text-xs text-sc-fg-muted mt-0.5">{description}</p>}
        </div>
        {children}
      </div>

      {error && (
        <p id={errorId} className="mt-2 text-sm text-sc-red" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

// Form section for grouping fields
interface FormSectionProps {
  title?: string;
  description?: string;
  children: ReactNode;
  className?: string;
}

export function FormSection({ title, description, children, className = '' }: FormSectionProps) {
  return (
    <fieldset className={`space-y-4 ${className}`}>
      {(title || description) && (
        <div className="mb-4">
          {title && <legend className="text-base font-semibold text-sc-fg-primary">{title}</legend>}
          {description && <p className="text-sm text-sc-fg-muted mt-1">{description}</p>}
        </div>
      )}
      {children}
    </fieldset>
  );
}
