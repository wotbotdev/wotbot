import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';

interface JobEventTriggerFieldsProps {
  eventName: string;
  subscriptionError: string | null;
  subscriptionInput: string;
  thingId: string;
  onEventNameChange: (value: string) => void;
  onSubscriptionInputChange: (value: string) => void;
  onThingIdChange: (value: string) => void;
}

export function JobEventTriggerFields({
  eventName,
  subscriptionError,
  subscriptionInput,
  thingId,
  onEventNameChange,
  onSubscriptionInputChange,
  onThingIdChange,
}: JobEventTriggerFieldsProps) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <div className="space-y-2">
        <label className="text-sm font-medium">Thing ID</label>
        <Input
          value={thingId}
          onChange={(event) => onThingIdChange(event.target.value)}
          placeholder="urn:logistics:scanner-7"
        />
      </div>
      <div className="space-y-2">
        <label className="text-sm font-medium">Event name</label>
        <Input
          value={eventName}
          onChange={(event) => onEventNameChange(event.target.value)}
          placeholder="parcelScanned"
        />
      </div>
      <div className="space-y-2 sm:col-span-2">
        <label className="text-sm font-medium">Subscription input JSON</label>
        <Textarea
          rows={5}
          value={subscriptionInput}
          onChange={(event) => onSubscriptionInputChange(event.target.value)}
          placeholder='{"threshold": 30}'
          aria-invalid={Boolean(subscriptionError)}
        />
        {subscriptionError ? (
          <p className="text-sm text-destructive">{subscriptionError}</p>
        ) : null}
      </div>
    </div>
  );
}
