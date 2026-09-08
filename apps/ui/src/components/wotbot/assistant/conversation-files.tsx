'use client';

import { useAuiState } from '@assistant-ui/react';
import { MessageSquare, Paperclip } from 'lucide-react';
import { useMemo, useRef, useState } from 'react';

import { Button } from '@/components/ui/button';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { artifactKey } from '@/components/wotbot/chat-tool-call-model';
import { FileArtifactCard } from '@/components/wotbot/chat-tool-calls/file-artifact-card';
import { conversationFiles, messageAnchor } from './artifacts';

/**
 * Downloadable files produced anywhere in the conversation.
 *
 * Only files are collected here. Charts, images, and panels stay in the
 * transcript, where the surrounding text explains them and the artifact cards
 * already offer a fullscreen view; files are the outputs that get scrolled
 * away from and wanted again later, and the only ones that outlive the turn
 * long enough for a standalone list to be honest about availability.
 */
export function ConversationFiles() {
  const messages = useAuiState((state) => state.thread.messages);
  const files = useMemo(() => conversationFiles(messages), [messages]);
  const [open, setOpen] = useState(false);
  // Radix restores focus to the trigger on close, which would scroll the
  // message we just jumped to back out of view; the jump waits for that.
  const pendingMessage = useRef<string | null>(null);

  if (!files.length) return null;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          className="mr-auto gap-2 text-muted-foreground"
          size="sm"
          type="button"
          variant="ghost"
        >
          <Paperclip className="size-3.5" aria-hidden />
          {files.length} file{files.length === 1 ? '' : 's'}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="max-h-80 w-[22rem] overflow-y-auto"
        onCloseAutoFocus={(event) => {
          const messageId = pendingMessage.current;
          pendingMessage.current = null;
          if (!messageId) return;
          const target = document.getElementById(messageAnchor(messageId));
          if (!target) return;
          event.preventDefault();
          target.scrollIntoView({ block: 'start', behavior: 'instant' });
          target.focus({ preventScroll: true });
        }}
      >
        {files.map(({ artifact, messageId }) => (
          <div className="space-y-1" key={artifactKey(artifact)}>
            <FileArtifactCard artifact={artifact} />
            <Button
              className="text-muted-foreground"
              onClick={() => {
                pendingMessage.current = messageId;
                setOpen(false);
              }}
              size="sm"
              type="button"
              variant="ghost"
            >
              <MessageSquare className="size-3.5" aria-hidden />
              Show in chat
            </Button>
          </div>
        ))}
      </PopoverContent>
    </Popover>
  );
}
