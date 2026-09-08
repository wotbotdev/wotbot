'use client';

import { type FormEvent, useCallback, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Plus } from 'lucide-react';
import { toast } from 'sonner';

import { Input } from '@/components/ui/input';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { JobActionFields } from '@/components/jobs/form/job-action-fields';
import { JobEventTriggerFields } from '@/components/jobs/form/job-event-trigger-fields';
import { JobFormCard } from '@/components/jobs/form/job-form-card';
import { FormPageHeader } from '@/components/form-page-header';
import {
  INITIAL_CREATE_JOB_FORM,
  type CreateJobFormState,
  type JobTriggerKind,
  getSubscriptionInputError,
  toCreateJobPayload,
  validateCreateJobForm,
} from '@/components/jobs/form/job-form-model';
import { JobScheduleFields } from '@/components/jobs/form/job-schedule-fields';
import { createJob } from '@/lib/jobs-api';

export function JobCreatePage() {
  const router = useRouter();
  const [form, setForm] = useState<CreateJobFormState>(INITIAL_CREATE_JOB_FORM);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const subscriptionError = useMemo(
    () => getSubscriptionInputError(form),
    [form],
  );

  const validationError = useMemo(() => validateCreateJobForm(form), [form]);

  const setField = useCallback(
    <K extends keyof CreateJobFormState>(
      field: K,
      value: CreateJobFormState[K],
    ) => {
      setForm((current) => ({ ...current, [field]: value }));
    },
    [],
  );

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (validationError || subscriptionError) return;
      setIsSubmitting(true);
      try {
        const job = await createJob(toCreateJobPayload(form));
        toast.success('Job created.');
        router.push(`/jobs/${job.id}`);
      } catch (error) {
        toast.error(
          error instanceof Error ? error.message : 'Failed to create job',
        );
      } finally {
        setIsSubmitting(false);
      }
    },
    [form, router, subscriptionError, validationError],
  );

  return (
    <form className="space-y-5" onSubmit={(event) => void handleSubmit(event)}>
      <FormPageHeader
        title="Create job"
        description="Define one action and one trigger for a background automation."
        cancelHref="/jobs"
        submitLabel="Create"
        submitIcon={<Plus className="h-4 w-4" />}
        isSubmitting={isSubmitting}
        disabled={
          isSubmitting || Boolean(validationError) || Boolean(subscriptionError)
        }
      />

      <JobFormCard
        title="Identity"
        description="Name the background automation."
      >
        <div className="space-y-2">
          <label className="text-sm font-medium">Name</label>
          <Input
            value={form.name}
            onChange={(event) => setField('name', event.target.value)}
            placeholder="Daily operations summary"
          />
        </div>
      </JobFormCard>

      <JobFormCard
        title="Action"
        description="Choose what the job should do each time it runs."
      >
        <JobActionFields
          actionKind={form.actionKind}
          prompt={form.prompt}
          analysisCode={form.analysisCode}
          onActionKindChange={(value) => setField('actionKind', value)}
          onPromptChange={(value) => setField('prompt', value)}
          onAnalysisCodeChange={(value) => setField('analysisCode', value)}
        />
      </JobFormCard>

      <JobFormCard
        title="Trigger"
        description="Pick whether this job runs on a schedule or from a Thing event."
      >
        <Tabs
          value={form.triggerKind}
          onValueChange={(value) =>
            setField('triggerKind', value as JobTriggerKind)
          }
          className="space-y-4"
        >
          <TabsList className="grid w-full grid-cols-2 sm:w-fit">
            <TabsTrigger value="time">Time</TabsTrigger>
            <TabsTrigger value="event">Event</TabsTrigger>
          </TabsList>

          <TabsContent value="time" className="mt-0">
            <JobScheduleFields
              scheduleKind={form.scheduleKind}
              intervalSeconds={form.intervalSeconds}
              runAt={form.runAt}
              cronExpression={form.cronExpression}
              cronTimezone={form.cronTimezone}
              onScheduleKindChange={(value) => setField('scheduleKind', value)}
              onIntervalSecondsChange={(value) =>
                setField('intervalSeconds', value)
              }
              onRunAtChange={(value) => setField('runAt', value)}
              onCronExpressionChange={(value) =>
                setField('cronExpression', value)
              }
              onCronTimezoneChange={(value) => setField('cronTimezone', value)}
            />
          </TabsContent>

          <TabsContent value="event" className="mt-0">
            <JobEventTriggerFields
              thingId={form.thingId}
              eventName={form.eventName}
              subscriptionInput={form.subscriptionInput}
              subscriptionError={subscriptionError}
              onThingIdChange={(value) => setField('thingId', value)}
              onEventNameChange={(value) => setField('eventName', value)}
              onSubscriptionInputChange={(value) =>
                setField('subscriptionInput', value)
              }
            />
          </TabsContent>
        </Tabs>
      </JobFormCard>

      {validationError ? (
        <p className="text-sm text-destructive">{validationError}</p>
      ) : null}
    </form>
  );
}
