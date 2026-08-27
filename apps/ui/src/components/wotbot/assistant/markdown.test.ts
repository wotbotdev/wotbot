import assert from 'node:assert/strict';
import test from 'node:test';

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';

import { markdownRemarkPlugins } from './markdown';

test('renders GitHub-flavored tables, task lists, and strikethrough', () => {
  const html = renderToStaticMarkup(
    createElement(
      ReactMarkdown,
      { remarkPlugins: markdownRemarkPlugins },
      [
        '| Device | State |',
        '| --- | --- |',
        '| Conveyor | Running |',
        '',
        '- [x] Checked',
        '',
        '~~obsolete~~',
      ].join('\n'),
    ),
  );

  assert.match(html, /<table>/);
  assert.match(html, /type="checkbox"/);
  assert.match(html, /<del>obsolete<\/del>/);
});
