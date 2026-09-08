'use client';

import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type ThreadMessageLike,
} from '@assistant-ui/react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { MessageSquareReply } from 'lucide-react';
import { toast } from 'sonner';

import { WotbotThread } from '@/components/wotbot/assistant/thread';
import {
  createThreadMessageConverter,
  identityThreadMessage,
  type LangChainMessage,
} from '@/lib/thread-messages';
import { JobEventTimeline } from '@/components/jobs/job-event-timeline';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { useJobEvents } from '@/hooks/use-job-events';
import { supportsJobReply } from '@/lib/job-formatters';
import {
  type JobRecord,
  type JobThreadRecord,
  fetchJobThread,
} from '@/lib/jobs-api';

export function normalizeMessages(
  thread: JobThreadRecord | null,
): LangChainMessage[] {
  return (thread?.messages ?? []).map((message, index) => {
    return {
      ...message,
      id: message.id ?? `job-message-${index}`,
    } as LangChainMessage;
  });
}

/**
 * Read-only transcript of a job thread.
 *
 * Job turns are produced by the worker, not typed here, so the runtime is fed
 * a fixed message list and the composer is replaced by a notice bar -- replies
 * happen on the Overview tab.
 */
export function JobTranscript({
  messages,
  jobId,
  isWaiting,
  waitingQuestion,
}: {
  messages: LangChainMessage[];
  jobId: string;
  isWaiting: boolean;
  waitingQuestion?: string | null;
}) {
  const [convertMessages] = useState(createThreadMessageConverter);
  const threadMessages = useMemo(
    () => convertMessages(messages),
    [convertMessages, messages],
  );

  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages: threadMessages,
    convertMessage: identityThreadMessage,
    isDisabled: true,
    onNew: async () => {
      // Read-only: the composer is replaced by the notice bar below.
    },
  });

  const footer = (
    <div className="mx-auto w-full max-w-3xl px-4 pb-4">
      <div className="flex flex-col gap-3 rounded-lg border border-dashed border-border bg-muted/20 px-4 py-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          {isWaiting ? (
            <>
              <div className="font-medium text-foreground">
                {waitingQuestion || 'The job is waiting for a reply.'}
              </div>
              <div>Answer this question from the Overview tab.</div>
            </>
          ) : (
            <div>Transcript view</div>
          )}
        </div>
        {isWaiting ? (
          <Button size="sm" asChild>
            <Link href={`/jobs/${jobId}`}>
              <MessageSquareReply className="h-4 w-4" />
              Answer
            </Link>
          </Button>
        ) : null}
      </div>
    </div>
  );

  return (
    <div className="min-h-[34rem] flex-1 overflow-hidden rounded-md border border-border/70 bg-background shadow-sm shadow-black/5">
      <AssistantRuntimeProvider runtime={runtime}>
        <WotbotThread className="wotbot-chat h-full" footer={footer} />
      </AssistantRuntimeProvider>
    </div>
  );
}

/**
 * Self-contained conversation view for the job detail "Conversation" tab.
 * Loads the job thread, live-refreshes on run events, and shows the message
 * transcript (falling back to the event timeline when there are no messages).
 */
export function JobConversationPanel({
  jobId,
  job,
}: {
  jobId: string;
  job: JobRecord;
}) {
  const [thread, setThread] = useState<JobThreadRecord | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(
    async ({ silent = false }: { silent?: boolean } = {}) => {
      if (!silent) setIsLoading(true);
      setLoadError(null);
      try {
        setThread(await fetchJobThread(jobId));
      } catch (error) {
        const message =
          error instanceof Error
            ? error.message
            : 'Failed to load conversation';
        setLoadError(message);
        if (!silent) toast.error(message);
      } finally {
        if (!silent) setIsLoading(false);
      }
    },
    [jobId],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useJobEvents(
    useCallback(
      (incoming: JobRecord) => {
        if (incoming.id === jobId) {
          void load({ silent: true });
        }
      },
      [jobId, load],
    ),
  );

  const messages = useMemo(() => normalizeMessages(thread), [thread]);
  const events = thread?.events ?? [];
  const isWaiting = supportsJobReply(job);

  if (isLoading && !thread) {
    return <Skeleton className="h-[34rem] rounded-md" />;
  }

  if (loadError && !thread) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Unable to load conversation</AlertTitle>
        <AlertDescription>{loadError}</AlertDescription>
      </Alert>
    );
  }

  if (messages.length === 0 && events.length === 0) {
    return (
      <Card className="rounded-md border-border/70 shadow-sm shadow-black/5">
        <CardContent className="flex min-h-40 items-center justify-center text-sm text-muted-foreground">
          No conversation yet.
        </CardContent>
      </Card>
    );
  }

  if (messages.length === 0) {
    return <JobEventTimeline events={events} />;
  }

  return (
    <JobTranscript
      messages={messages}
      jobId={jobId}
      isWaiting={isWaiting}
      waitingQuestion={job.waiting_question}
    />
  );
}
