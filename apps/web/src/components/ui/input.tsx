import {
  forwardRef,
  type InputHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from 'react';

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  icon?: ReactNode;
  error?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ icon, error, className = '', ...props }, ref) => {
    return (
      <div className="relative">
        {icon && (
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sc-fg-muted pointer-events-none">
            {icon}
          </span>
        )}
        <input
          ref={ref}
          className={`
            w-full bg-sc-bg-highlight border rounded-lg
            text-sc-fg-primary placeholder:text-sc-fg-subtle
            transition-colors duration-150
            focus:outline-none focus:border-sc-cyan focus:ring-1 focus:ring-sc-cyan/20
            disabled:opacity-50 disabled:cursor-not-allowed
            ${icon ? 'pl-10' : 'pl-4'} pr-4 py-2
            ${error ? 'border-sc-red' : 'border-sc-fg-subtle/20'}
            ${className}
          `}
          {...props}
        />
        {error && <p className="mt-1.5 text-sm text-sc-red">{error}</p>}
      </div>
    );
  }
);

Input.displayName = 'Input';

// Search-specific input with larger styling
interface SearchInputProps extends InputProps {
  onSubmit?: () => void;
}

export const SearchInput = forwardRef<HTMLInputElement, SearchInputProps>(
  ({ onSubmit, className = '', ...props }, ref) => {
    return (
      <div className="relative">
        <span className="absolute left-4 top-1/2 -translate-y-1/2 text-sc-fg-muted text-lg pointer-events-none">
          ⌕
        </span>
        <input
          ref={ref}
          type="text"
          className={`
            w-full pl-12 pr-4 py-3 bg-sc-bg-base border border-sc-fg-subtle/20 rounded-xl
            text-lg text-sc-fg-primary placeholder:text-sc-fg-subtle
            transition-all duration-200
            focus:outline-none focus:border-sc-purple focus:ring-2 focus:ring-sc-purple/10
            hover:border-sc-fg-subtle/40
            ${className}
          `}
          onKeyDown={e => {
            if (e.key === 'Enter' && onSubmit) onSubmit();
          }}
          {...props}
        />
      </div>
    );
  }
);

SearchInput.displayName = 'SearchInput';

// Textarea component
interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  error?: string;
  monospace?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ error, monospace = false, className = '', ...props }, ref) => {
    return (
      <div>
        <textarea
          ref={ref}
          className={`
            w-full bg-sc-bg-highlight border rounded-lg px-4 py-3
            text-sc-fg-primary placeholder:text-sc-fg-subtle
            transition-colors duration-150 resize-none
            focus:outline-none focus:border-sc-cyan focus:ring-1 focus:ring-sc-cyan/20
            disabled:opacity-50 disabled:cursor-not-allowed
            ${monospace ? 'font-mono text-sm' : ''}
            ${error ? 'border-sc-red' : 'border-sc-fg-subtle/20'}
            ${className}
          `}
          {...props}
        />
        {error && <p className="mt-1.5 text-sm text-sc-red">{error}</p>}
      </div>
    );
  }
);

Textarea.displayName = 'Textarea';

// Label component
interface LabelProps {
  htmlFor?: string;
  children: ReactNode;
  description?: string;
  required?: boolean;
}

export function Label({ htmlFor, children, description, required }: LabelProps) {
  return (
    <div className="mb-2">
      <label htmlFor={htmlFor} className="block text-sm font-medium text-sc-fg-muted">
        {children}
        {required && <span className="text-sc-red ml-1">*</span>}
      </label>
      {description && <p className="text-xs text-sc-fg-subtle mt-0.5">{description}</p>}
    </div>
  );
}
