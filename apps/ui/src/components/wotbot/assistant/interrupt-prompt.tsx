'use client';

import { DatabaseZap, KeyRound } from 'lucide-react';
import { useState, type ReactNode } from 'react';

import { SourceRegistrationDialog } from '@/components/sources/source-registration-dialog';
import { CredentialDialog } from '@/components/things/thing-detail-credential-dialog';
import { Button } from '@/components/ui/button';
import type { CredentialChallenge } from '@/components/wotbot/assistant/credential-interrupt-card';
import type { SourceDraft } from '@/lib/sources-api';

/**
 * The prompt for a run suspended on an interrupt.
 *
 * The form stays in a dialog: registering a source probes a URL and writes to
 * the source registry, and a credential is a secret, so neither belongs in a
 * message bubble. What goes in the transcript is one row marking where the run
 * stopped, so the ask is anchored to the turn that raised it instead of
 * floating above the whole conversation, and so the dialog can be reopened
 * after it is dismissed.
 */
function PromptRow({
  action,
  icon,
  message,
}: {
  action: ReactNode;
  icon: ReactNode;
  message: string;
}) {
  return (
    <div className="my-2 flex items-center gap-3 rounded-lg border border-border bg-muted/40 px-3 py-2 text-sm">
      <span className="shrink-0 text-muted-foreground [&_svg]:size-4">
        {icon}
      </span>
      {/* A draft URL can be arbitrarily long; keep it off the buttons. */}
      <span
        className="min-w-0 flex-1 truncate text-muted-foreground"
        title={message}
      >
        {message}
      </span>
      <span className="shrink-0">{action}</span>
    </div>
  );
}

/** Guards the one-shot resume so a double click cannot submit it twice. */
function useResume() {
  const [resuming, setResuming] = useState(false);
  return {
    resuming,
    run: (callback: () => Promise<void>) => {
      if (resuming) return;
      setResuming(true);
      void callback().finally(() => setResuming(false));
    },
  };
}

export function SourceRegistrationPrompt({
  draft,
  onCancel,
  onRegistered,
}: {
  draft: SourceDraft;
  onCancel: () => Promise<void>;
  onRegistered: (sourceId: string, thingId?: string) => Promise<void>;
}) {
  // Opens on arrival because the run is blocked on the answer, then stays
  // reopenable from the row if it is dismissed.
  const [open, setOpen] = useState(true);
  const { resuming, run } = useResume();
  const label = draft.url ?? draft.provider ?? 'a new source';

  return (
    <>
      <PromptRow
        icon={<DatabaseZap />}
        message={
          resuming
            ? 'Continuing...'
            : `Waiting for you to confirm registering ${label}.`
        }
        action={
          <div className="flex gap-2">
            <Button size="sm" onClick={() => setOpen(true)} disabled={resuming}>
              Review
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => run(onCancel)}
              disabled={resuming}
            >
              Cancel
            </Button>
          </div>
        }
      />
      <SourceRegistrationDialog
        open={open}
        onOpenChange={setOpen}
        initialDraft={draft}
        onRegistered={(source, onboarding) =>
          run(() => onRegistered(source.source_id, onboarding?.thing?.id))
        }
      />
    </>
  );
}

export function CredentialPrompt({
  challenge,
  onCancel,
  onSaved,
}: {
  challenge: CredentialChallenge;
  onCancel: () => Promise<void>;
  onSaved: () => Promise<void>;
}) {
  const [open, setOpen] = useState(true);
  const { resuming, run } = useResume();
  const owner = challenge.owner_kind === 'source' ? 'source' : 'Thing';

  return (
    <>
      <PromptRow
        icon={<KeyRound />}
        message={
          resuming
            ? 'Continuing...'
            : challenge.message ||
              `Waiting for ${challenge.scheme} credentials for this ${owner}.`
        }
        action={
          <div className="flex gap-2">
            <Button size="sm" onClick={() => setOpen(true)} disabled={resuming}>
              Add credentials
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => run(onCancel)}
              disabled={resuming}
            >
              Cancel
            </Button>
          </div>
        }
      />
      <CredentialDialog
        open={open}
        onOpenChange={setOpen}
        thingId={challenge.thing_id}
        sourceId={challenge.source_id}
        secDef={{ name: challenge.security_name, scheme: challenge.scheme }}
        onSaved={() => run(onSaved)}
      />
    </>
  );
}
