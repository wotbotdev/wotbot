'use client';

import { useMemo } from 'react';

import { parseCredentialChallenge } from '@/components/wotbot/assistant/credential-interrupt-card';
import {
  CredentialPrompt,
  SourceRegistrationPrompt,
} from '@/components/wotbot/assistant/interrupt-prompt';
import { parseSourceRegistrationInterrupt } from '@/components/wotbot/assistant/source-registration-interrupt-card';
import type { useWotbotRuntime } from '@/components/wotbot/assistant/use-wotbot-runtime';

/** The same registration and credential approvals in full and embedded chats. */
export function useChatInterruptPrompt(
  stream: ReturnType<typeof useWotbotRuntime>['stream'],
) {
  const credentialChallenge = useMemo(
    () =>
      stream.interrupts
        .map((item) => parseCredentialChallenge(item.value))
        .find((item) => item !== null) ?? null,
    [stream.interrupts],
  );
  const sourceRegistration = useMemo(
    () =>
      stream.interrupts
        .map((item) => parseSourceRegistrationInterrupt(item.value))
        .find((item) => item !== null) ?? null,
    [stream.interrupts],
  );

  if (credentialChallenge) {
    const resume = (status: 'credential_saved' | 'credential_cancelled') =>
      stream.submit(null, { command: { resume: { status } } });
    return (
      <CredentialPrompt
        challenge={credentialChallenge}
        onCancel={() => resume('credential_cancelled')}
        onSaved={() => resume('credential_saved')}
      />
    );
  }
  if (sourceRegistration) {
    return (
      <SourceRegistrationPrompt
        draft={sourceRegistration.draft}
        onCancel={() =>
          stream.submit(null, {
            command: { resume: { status: 'source_registration_cancelled' } },
          })
        }
        onRegistered={(sourceId, thingId) =>
          stream.submit(null, {
            command: {
              resume: {
                status: 'source_registered',
                source_id: sourceId,
                ...(thingId ? { thing_id: thingId } : {}),
              },
            },
          })
        }
      />
    );
  }
  return null;
}
