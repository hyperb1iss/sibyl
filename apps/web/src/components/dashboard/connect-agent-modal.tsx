'use client';

import { ConnectPanel } from '@/components/connect';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Network } from '@/components/ui/icons';

interface ConnectAgentModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ConnectAgentModal({ open, onOpenChange }: ConnectAgentModalProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center shadow-glow-purple">
              <Network aria-hidden="true" width={20} height={20} className="text-sc-on-accent" />
            </div>
            <div>
              <DialogTitle>Connect your tools</DialogTitle>
              <DialogDescription>
                One line in your terminal, or one for your agent
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="my-6">
          <ConnectPanel />
        </div>

        <div className="flex justify-end">
          <Button type="button" variant="primary" onClick={() => onOpenChange(false)}>
            Done
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
