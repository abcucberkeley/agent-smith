import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'agent-smith',
  component: 'Pane',
  requestId: 'agent-smith',
  props: {
    title: 'Agent Smith',
    isFocused: false,
    bodyColumns: 120,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

test('the pane draws its sections on terminal and desktop before data arrives', async ($, on) => {
  on('clock.now', () => ({ value: Date.UTC(2026, 9, 27) }))
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /USAGE/ }))?.text).toContain('USAGE')
    expect((await ui.find({ type: 'Text', text: /fetching/ }))?.text).toContain('fetching')
    expect((await ui.find({ type: 'Text', text: /CLAUDE AGENTS/ }))?.text).toContain('0')
    expect((await ui.find({ type: 'Text', text: /no background agents/ }))).toBeTruthy()
    await ui.unmount()
  }
})
