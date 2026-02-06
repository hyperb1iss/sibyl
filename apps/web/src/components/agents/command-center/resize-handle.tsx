'use client';

import { Separator } from 'react-resizable-panels';

interface ResizeHandleProps {
  className?: string;
}

export function ResizeHandle({ className }: ResizeHandleProps) {
  return (
    <Separator
      className={`group relative flex items-center justify-center w-1.5 hover:w-2 transition-all duration-150 ${className ?? ''}`}
    >
      {/* Visible line */}
      <div className="absolute inset-y-0 w-px bg-sc-fg-subtle/20 group-hover:w-[3px] group-hover:bg-sc-purple/40 group-data-[resize-handle-active]:w-[3px] group-data-[resize-handle-active]:bg-sc-purple/60 transition-all duration-150 rounded-full" />
    </Separator>
  );
}
