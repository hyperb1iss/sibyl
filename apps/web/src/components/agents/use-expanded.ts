/**
 * Hook for expandable/collapsible UI patterns.
 *
 * Provides consistent expansion state and animated CSS classes
 * for collapsible content blocks.
 */

import { useCallback, useState } from 'react';

// =============================================================================
// Types
// =============================================================================

/** Predefined max-height sizes for expandable content */
export type ExpandedSize = 'sm' | 'md' | 'lg' | 'xl';

export interface UseExpandedOptions {
  /** Initial expanded state (default: false) */
  initialExpanded?: boolean;
}

export interface UseExpandedReturn {
  /** Current expansion state */
  isExpanded: boolean;
  /** Set expansion state directly */
  setIsExpanded: React.Dispatch<React.SetStateAction<boolean>>;
  /** Toggle expansion state */
  toggle: () => void;
}

// =============================================================================
// Size Mappings (static for Tailwind)
// =============================================================================

/**
 * Map of size names to max-height classes.
 * These are statically defined so Tailwind can analyze them.
 */
const EXPANDED_CLASSES: Record<ExpandedSize, string> = {
  sm: 'max-h-[300px]',
  md: 'max-h-[500px]',
  lg: 'max-h-[600px]',
  xl: 'max-h-[800px]',
};

/** Base classes for all expandable containers */
const BASE_CLASSES = 'overflow-hidden transition-all duration-300 ease-out';
const COLLAPSED_CLASSES = 'max-h-0 opacity-0';
const EXPANDED_OPACITY = 'opacity-100';

// =============================================================================
// Hook
// =============================================================================

/**
 * Hook for managing expandable content state.
 *
 * Usage:
 * ```tsx
 * const { isExpanded, toggle } = useExpanded();
 *
 * return (
 *   <>
 *     <button onClick={toggle}>Toggle</button>
 *     <div className={getExpandedClasses(isExpanded)}>
 *       <div className="p-4">Content here</div>
 *     </div>
 *   </>
 * );
 * ```
 */
export function useExpanded(options: UseExpandedOptions = {}): UseExpandedReturn {
  const { initialExpanded = false } = options;

  const [isExpanded, setIsExpanded] = useState(initialExpanded);

  const toggle = useCallback(() => {
    setIsExpanded(prev => !prev);
  }, []);

  return {
    isExpanded,
    setIsExpanded,
    toggle,
  };
}

// =============================================================================
// CSS Class Helpers
// =============================================================================

/**
 * Generate CSS classes for expandable container.
 * Uses predefined sizes for Tailwind compatibility.
 *
 * @param isExpanded - Current expansion state
 * @param size - Max height size (sm: 300px, md: 500px, lg: 600px, xl: 800px)
 */
export function getExpandedClasses(isExpanded: boolean, size: ExpandedSize = 'md'): string {
  if (isExpanded) {
    return `${BASE_CLASSES} ${EXPANDED_CLASSES[size]} ${EXPANDED_OPACITY}`;
  }
  return `${BASE_CLASSES} ${COLLAPSED_CLASSES}`;
}
