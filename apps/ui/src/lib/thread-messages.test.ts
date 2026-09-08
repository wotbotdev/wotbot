import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ARTIFACT_VIEW_NAME,
  WOT_SUMMARY_NAME,
  createThreadMessageConverter,
  toThreadMessages,
  type LangChainMessage,
} from './thread-messages';

type AnyPart = { type: string; [key: string]: unknown };

function parts(message: unknown): AnyPart[] {
  return (message as { content: AnyPart[] }).content;
}

/**
 * A message's tool calls, in order.
 *
 * They are ordinary parts now rather than one synthetic group part; grouping
 * happens structurally at render time via `wotbotGroupBy`.
 */
function groupCalls(
  message: unknown,
): Array<{ id: string; name: string } & Record<string, unknown>> {
  return parts(message)
    .filter((part) => part.type === 'tool-call')
    .map((part) => {
      const { toolCallId, toolName, ...rest } = part;
      return { id: toolCallId as string, name: toolName as string, ...rest };
    });
}

test('maps a human message to a user message with a text part', () => {
  const out = toThreadMessages([{ type: 'human', content: 'hello', id: 'h1' }]);
  assert.equal(out.length, 1);
  assert.equal(out[0].role, 'user');
  assert.deepEqual(parts(out[0]), [{ type: 'text', text: 'hello' }]);
  assert.equal((out[0] as { id?: string }).id, 'h1');
});

test('folds a tool message back onto the call it answers', () => {
  const messages: LangChainMessage[] = [
    { type: 'human', content: 'run it' },
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'call_1', name: 'run_code', args: { code: 'x=1' } }],
    },
    { type: 'tool', tool_call_id: 'call_1', content: '{"stdout":"ok"}' },
  ];

  const out = toThreadMessages(messages);
  // The tool result must NOT become its own message.
  assert.equal(out.length, 2);
  const [call] = groupCalls(out[1]);
  assert.equal(call.id, 'call_1');
  assert.equal(call.name, 'run_code');
  assert.deepEqual(call.args, { code: 'x=1' });
  // Decoded, because the cards downstream read fields off the object.
  assert.deepEqual(call.result, { stdout: 'ok' });
});

test('keeps a non-JSON tool result as a raw string', () => {
  const out = toThreadMessages([
    { type: 'ai', content: '', tool_calls: [{ id: 'c', name: 't', args: {} }] },
    { type: 'tool', tool_call_id: 'c', content: 'plain text' },
  ]);
  assert.equal(groupCalls(out[0])[0].result, 'plain text');
});

test('marks an errored tool result', () => {
  const out = toThreadMessages([
    { type: 'ai', content: '', tool_calls: [{ id: 'c', name: 't', args: {} }] },
    { type: 'tool', tool_call_id: 'c', content: 'boom', status: 'error' },
  ]);
  assert.equal(groupCalls(out[0])[0].isError, true);
});

test('a whole turn becomes one message with its parts in order', () => {
  const out = toThreadMessages([
    { type: 'human', content: 'go' },
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'a', name: 't1', args: {} }],
    },
    { type: 'tool', tool_call_id: 'a', content: '{"ok":1}' },
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'b', name: 't2', args: {} }],
    },
    { type: 'tool', tool_call_id: 'b', content: '{"ok":2}' },
    { type: 'ai', content: 'all done' },
  ]);

  // Each agent step is its own LangChain message but one turn on screen, so
  // the run's shape is decided at render time by grouping, not here.
  assert.equal(out.length, 2);
  const calls = groupCalls(out[1]);
  assert.deepEqual(
    calls.map((c) => c.id),
    ['a', 'b'],
  );
  assert.deepEqual(
    calls.map((c) => c.result),
    [{ ok: 1 }, { ok: 2 }],
  );
  assert.deepEqual(
    parts(out[1]).map((part) => part.type),
    ['tool-call', 'tool-call', 'text'],
  );
});

test("text keeps its position among the turn's tool calls", () => {
  const out = toThreadMessages([
    {
      type: 'ai',
      content: 'thinking',
      tool_calls: [{ id: 'a', name: 't1', args: {} }],
    },
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'b', name: 't2', args: {} }],
    },
  ]);

  assert.equal(out.length, 1);
  assert.deepEqual(
    parts(out[0]).map((part) => part.type),
    ['text', 'tool-call', 'tool-call'],
  );
  assert.deepEqual(
    groupCalls(out[0]).map((c) => c.id),
    ['a', 'b'],
  );
});

test('a user message breaks the run', () => {
  const out = toThreadMessages([
    { type: 'ai', content: '', tool_calls: [{ id: 'a', name: 't', args: {} }] },
    { type: 'human', content: 'wait' },
    { type: 'ai', content: '', tool_calls: [{ id: 'b', name: 't', args: {} }] },
  ]);
  assert.equal(out.length, 3);
  assert.deepEqual(
    groupCalls(out[0]).map((c) => c.id),
    ['a'],
  );
  assert.equal(out[1].role, 'user');
  assert.deepEqual(
    groupCalls(out[2]).map((c) => c.id),
    ['b'],
  );
});

test('parallel calls in one turn share a group', () => {
  const out = toThreadMessages([
    {
      type: 'ai',
      content: '',
      tool_calls: [
        { id: 'p1', name: 't', args: {} },
        { id: 'p2', name: 't', args: {} },
      ],
    },
  ]);
  assert.deepEqual(
    groupCalls(out[0]).map((c) => c.id),
    ['p1', 'p2'],
  );
});

test('an unanswered call has no result and still renders', () => {
  const out = toThreadMessages([
    {
      type: 'ai',
      content: 'working',
      tool_calls: [{ id: 'c', name: 'run_code', args: {} }],
    },
  ]);
  const [text] = parts(out[0]);
  assert.deepEqual(text, { type: 'text', text: 'working' });
  const [call] = groupCalls(out[0]);
  assert.equal('result' in call, false);
});

test('preserves reasoning blocks as reasoning, not visible text', () => {
  const out = toThreadMessages([
    {
      type: 'ai',
      content: [
        { type: 'reasoning', text: 'thinking...' },
        { type: 'text', text: 'answer' },
      ],
    },
  ]);
  assert.deepEqual(parts(out[0]), [
    { type: 'reasoning', text: 'thinking...' },
    { type: 'text', text: 'answer' },
  ]);
});

test('accepts streaming chunk type from messages mode', () => {
  const out = toThreadMessages([
    { type: 'AIMessageChunk', content: 'partial' },
  ]);
  assert.equal(out.length, 1);
  assert.equal(out[0].role, 'assistant');
});

test('skips empty assistant turns so no blank bubble renders', () => {
  assert.deepEqual(toThreadMessages([{ type: 'ai', content: '' }]), []);
});

test('keeps system messages (job run-lifecycle lines) and drops orphan tool results', () => {
  const out = toThreadMessages([
    { type: 'system', content: 'Run started.' },
    { type: 'tool', tool_call_id: 'missing', content: '{}' },
    { type: 'human', content: 'hi' },
  ]);
  assert.equal(out.length, 2);
  assert.equal(out[0].role, 'system');
  assert.deepEqual(parts(out[0]), [{ type: 'text', text: 'Run started.' }]);
  assert.equal(out[1].role, 'user');
});

test('handles empty and undefined input', () => {
  assert.deepEqual(toThreadMessages([]), []);
  assert.deepEqual(toThreadMessages(undefined), []);
});

test('hides a summary-shaped turn that does not parse', () => {
  const out = toThreadMessages([
    { type: 'ai', content: '{"type":"wotbot_device_interactions","bad"' },
  ]);
  assert.deepEqual(out, []);
});

test('renders a device-interaction summary as its own part', () => {
  const summary = JSON.stringify({
    type: 'wotbot_device_interactions',
    interactions: [
      {
        affordanceName: 'running',
        ok: true,
        thingId: 'conveyor',
        type: 'property',
      },
    ],
  });
  const out = toThreadMessages([{ type: 'ai', content: summary, id: 'a1' }]);
  assert.equal(out.length, 1);
  const [part] = parts(out[0]);
  assert.equal(part.type, 'tool-call');
  assert.equal(part.toolName, WOT_SUMMARY_NAME);
  assert.ok(
    ((part.args as { interactions: unknown[] }).interactions ?? []).length > 0,
  );
});

test('reasoning reported beside content becomes a reasoning part', () => {
  const [message] = toThreadMessages([
    {
      type: 'ai',
      content: 'The answer is 391.',
      additional_kwargs: { reasoning: '17 x 23 = 391' },
    },
  ]);

  assert.equal(message.role, 'assistant');
  assert.deepEqual(message.content, [
    { type: 'reasoning', text: '17 x 23 = 391' },
    { type: 'text', text: 'The answer is 391.' },
  ]);
});

test('blank or absent detached reasoning adds no part', () => {
  for (const additional_kwargs of [
    { reasoning: '   ' },
    { reasoning: 42 },
    {},
  ]) {
    const [message] = toThreadMessages([
      { type: 'ai', content: 'Hi.', additional_kwargs },
    ]);
    assert.deepEqual(message.content, [{ type: 'text', text: 'Hi.' }]);
  }
});

test('detached reasoning does not split a coalesced tool run', () => {
  const messages = toThreadMessages([
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'a', name: 'one', args: {} }],
    },
    {
      type: 'ai',
      content: '',
      additional_kwargs: { reasoning: 'still working' },
      tool_calls: [{ id: 'b', name: 'two', args: {} }],
    },
  ]);

  assert.equal(messages.length, 1);
});

test('every step of a tool loop keeps its reasoning', () => {
  // Each step is its own LangChain message; only the last used to survive.
  const messages = toThreadMessages([
    {
      type: 'ai',
      content: '',
      additional_kwargs: { reasoning: 'step one' },
      tool_calls: [{ id: 'a', name: 'one', args: {} }],
    },
    {
      type: 'ai',
      content: '',
      additional_kwargs: { reasoning: 'step two' },
      tool_calls: [{ id: 'b', name: 'two', args: {} }],
    },
    {
      type: 'ai',
      content: '',
      additional_kwargs: { reasoning: 'step three' },
      tool_calls: [{ id: 'c', name: 'three', args: {} }],
    },
  ]);

  const reasoning = messages
    .flatMap(
      (message) => message.content as Array<{ type: string; text?: string }>,
    )
    .filter((part) => part.type === 'reasoning')
    .map((part) => part.text);

  assert.deepEqual(reasoning, ['step one', 'step two', 'step three']);
  assert.equal(messages.length, 1);
});

test('reasoning after a run closes is not appended to the closed run', () => {
  const messages = toThreadMessages([
    {
      type: 'ai',
      content: '',
      additional_kwargs: { reasoning: 'during' },
      tool_calls: [{ id: 'a', name: 'one', args: {} }],
    },
    { type: 'ai', content: 'Done.' },
    { type: 'human', content: 'again' },
    {
      type: 'ai',
      content: 'Second answer.',
      additional_kwargs: { reasoning: 'after' },
    },
  ]);

  const last = messages[messages.length - 1];
  assert.deepEqual(last.content, [
    { type: 'reasoning', text: 'after' },
    { type: 'text', text: 'Second answer.' },
  ]);
});

test('an artifact is split off so the card can stay grouped', () => {
  const out = toThreadMessages([
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'a', name: 'create_web_interface', args: { x: 1 } }],
    },
    { type: 'tool', tool_call_id: 'a', content: '{"artifact":{"id":"art-1"}}' },
    { type: 'ai', content: 'Panel is above.' },
  ]);

  const kinds = parts(out[0]).map((part) => [part.type, part.toolName]);
  // The artifact sits between the work and the answer that refers to it.
  assert.deepEqual(kinds, [
    ['tool-call', 'create_web_interface'],
    ['tool-call', ARTIFACT_VIEW_NAME],
    ['text', undefined],
  ]);

  const artifact = parts(out[0])[1];
  assert.deepEqual(artifact.args, {
    source: 'create_web_interface',
    sourceArgs: { x: 1 },
  });
});

test('a call still awaiting its result produces no artifact part', () => {
  const out = toThreadMessages([
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'a', name: 'run_code', args: {} }],
    },
  ]);

  assert.equal(
    parts(out[0]).some((part) => part.toolName === ARTIFACT_VIEW_NAME),
    false,
  );
});

test('the summary does not push artifacts past the answer', () => {
  const out = toThreadMessages([
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'a', name: 'create_web_interface', args: {} }],
    },
    { type: 'tool', tool_call_id: 'a', content: '{"artifact":{"id":"art-1"}}' },
    { type: 'ai', content: 'The panel is above.' },
    {
      type: 'ai',
      content: JSON.stringify({
        type: 'wotbot_device_interactions',
        interactions: [
          {
            affordanceName: 'running',
            ok: true,
            thingId: 'conveyor',
            type: 'property',
          },
        ],
      }),
    },
  ]);

  assert.deepEqual(
    parts(out[0]).map((part) => part.toolName ?? part.type),
    ['create_web_interface', ARTIFACT_VIEW_NAME, 'text', WOT_SUMMARY_NAME],
  );
});

test('each artifact sits with the run that produced it', () => {
  // Two artifacts with prose between them: stacking both after the last call
  // would put the first below the sentence that says it is above.
  const out = toThreadMessages([
    {
      type: 'ai',
      content: '',
      tool_calls: [{ id: 'a', name: 'create_web_interface', args: {} }],
    },
    { type: 'tool', tool_call_id: 'a', content: '{"artifact":{"id":"art-a"}}' },
    {
      type: 'ai',
      content: 'The panel above shows the conveyor.',
      tool_calls: [{ id: 'b', name: 'create_web_interface', args: {} }],
    },
    { type: 'tool', tool_call_id: 'b', content: '{"artifact":{"id":"art-b"}}' },
    { type: 'ai', content: 'And this one shows the fan.' },
  ]);

  assert.deepEqual(
    parts(out[0]).map((part) => part.toolName ?? part.type),
    [
      'create_web_interface',
      ARTIFACT_VIEW_NAME,
      'text',
      'create_web_interface',
      ARTIFACT_VIEW_NAME,
      'text',
    ],
  );
});

test('a tool call with no id is skipped rather than left unanswerable', () => {
  // Its result arrives keyed by the provider's id, so a synthesized one would
  // never match and the card would sit at "executing" forever.
  const out = toThreadMessages([
    {
      type: 'ai',
      content: 'working',
      tool_calls: [
        { name: 'no_id_tool', args: {} },
        { id: 'b', name: 'real_tool', args: {} },
      ],
    },
  ]);

  assert.deepEqual(
    groupCalls(out[0]).map((call) => call.id),
    ['b'],
  );
});

test('streaming preserves completed messages, tool results and unchanged active parts', () => {
  const convert = createThreadMessageConverter();
  const history: LangChainMessage[] = [
    { type: 'human', id: 'h1', content: 'First question' },
    { type: 'ai', id: 'a1', content: 'First answer' },
    { type: 'human', id: 'h2', content: 'Export readings' },
    {
      type: 'ai',
      id: 'a2',
      tool_calls: [{ id: 'c', name: 'run_code', args: { code: 'export()' } }],
    },
    {
      type: 'tool',
      tool_call_id: 'c',
      content:
        '{"stdout":"Exported","artifacts":[{"kind":"file","ref":"file_1","filename":"readings.csv","id":"file-1"}]}',
    },
    { type: 'ai', id: 'a3', content: 'Saved' },
  ];
  const first = convert(history);
  assert.strictEqual(convert([...history]), first);
  const next = convert([
    ...history.slice(0, -1),
    { ...history.at(-1), content: 'Saved the readings.' },
  ]);
  for (let i = 0; i < 3; i++) assert.strictEqual(next[i], first[i]);
  assert.notStrictEqual(next[3], first[3]);
  assert.strictEqual(parts(next[3])[0], parts(first[3])[0]);
  assert.strictEqual(parts(next[3])[1], parts(first[3])[1]);
  assert.strictEqual(
    groupCalls(next[3])[0].result,
    groupCalls(first[3])[0].result,
  );
  assert.equal(parts(first[3]).at(-1)?.text, 'Saved');
});

test('cached conversion matches fresh conversion through streaming, edits, recovery and late results', () => {
  const convert = createThreadMessageConverter();
  const history: LangChainMessage[] = [
    { type: 'system', id: 's1', content: 'Run started' },
    { type: 'human', id: 'h1', content: 'Read a device' },
    {
      type: 'ai',
      id: 'a1',
      content: [{ type: 'reasoning', text: 'Inspect it' }],
      tool_calls: [{ id: 'c1', name: 'run_code', args: {} }],
    },
    {
      type: 'tool',
      tool_call_id: 'c1',
      status: 'error',
      content: '{"error":"failed"}',
    },
    {
      type: 'ai',
      content:
        '{"type":"wotbot_device_interactions","interactions":[{"type":"read_property","thing_id":"urn:meter","name":"power"}]}',
    },
    { type: 'human', id: 'h2', content: 'Next question' },
    {
      type: 'ai',
      id: 'a2',
      content: 'Answer',
      additional_kwargs: { reasoning: 'Think first' },
    },
    // A late result must update its earlier call without mutating cached state.
    { type: 'tool', tool_call_id: 'c1', content: '{"stdout":"late result"}' },
  ];
  const snapshots = [
    ...history.map((_, i) => history.slice(0, i + 1)),
    history.slice(0, 3),
    [
      ...history.slice(0, 3),
      { type: 'tool', tool_call_id: 'c1', content: '{"stdout":"retried"}' },
    ],
    structuredClone(history),
    [
      { type: 'human', id: 'h1', content: 'Edited question' },
      ...history.slice(2),
    ],
    [],
    history,
  ];
  for (const snapshot of snapshots) {
    const before = convert(snapshot);
    const saved = structuredClone(before);
    assert.deepEqual(before, toThreadMessages(snapshot));
    convert(history);
    assert.deepEqual(
      before,
      saved,
      'a later conversion must not mutate an earlier snapshot',
    );
  }
});
